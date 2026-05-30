"""Tests for orphan-reconciliation in the frame-merge layer.

When a frame's owning writer process dies mid-flight (signal, crash, or
project-directory move), any non-terminal node states stay pinned at
``awaiting_*`` forever. Unwind reads those as ``status="running"`` and
shows an in-progress spinner indefinitely.

The merge layer (`frames._reconcile_orphan_states`) detects this by
checking the frame's recorded `writer_pid` against process-table
liveness; when dead, non-terminal kinds are rewritten to the synthetic
terminal kind ``"abandoned"`` so the merged report (and unwind) settle.

Frame writing, loading, and merging here go through the public
`InvocationReport` boundary (`write_frame` / `load_frames` /
`merged_document`); the reconciliation that those operations trigger is
the behavior under test. A couple of tests reach into `frames._pid_alive`
directly — that liveness probe is the internal primitive being
exercised, with no honest expression through the report boundary.
"""

from __future__ import annotations

import os
import subprocess
import sys

import agent_callstack as ac
import yaml
from agent_callstack import ROOT_FRAME_KEY, InvocationReport, frames as frames_mod, state as st
from agent_callstack.driver import Node, Tree
from agent_callstack.session import SessionRef


def _running_node(nid: str, task: str, *, session_id: str | None = None) -> Node:
    """A node currently in awaiting_turn (status='running') — the shape
    that gets stuck after a crash."""
    sid = session_id or f"sess-{nid}"
    return Node(
        id=nid,
        task=task,
        state=st.AwaitingTurn(session_id=sid),
    )


def _write_frame(report: InvocationReport, *, frame_key: str, tree: Tree, writer_pid: int | None) -> None:
    frame: dict = {
        "frame_key": frame_key,
        "is_nested": frame_key != ROOT_FRAME_KEY,
        "kind": "call",
        "tasks": ["t"],
        "cwd": "/cwd",
        "started_at": "s",
        "ended_at": "e",
        "tree": tree.to_dict(),
    }
    if writer_pid is not None:
        frame["writer_pid"] = writer_pid
    report.write_frame(frame, key=frame_key)


def _dead_pid() -> int:
    """A PID we can confidently say is not alive: spawn+reap a short-lived
    child via subprocess, then return its now-dead pid. Real OS state, not
    a monkeypatch — proves the production `os.kill(pid, 0)` probe actually
    catches the abandoned case rather than being a no-op. Uses subprocess
    rather than `os.fork()` to avoid the multi-threaded-fork deprecation
    warning when pytest itself runs threads."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# ---------- _pid_alive probe ----------


def test_pid_alive_returns_true_for_self():
    assert frames_mod._pid_alive(os.getpid()) is True


def test_pid_alive_returns_false_for_reaped_child():
    pid = _dead_pid()
    assert frames_mod._pid_alive(pid) is False


def test_pid_alive_rejects_invalid_pids():
    # Defensive: invalid pids must not crash and must not be reported alive
    # (otherwise reconciliation would skip every frame with a corrupted pid).
    assert frames_mod._pid_alive(0) is False
    assert frames_mod._pid_alive(-1) is False
    assert frames_mod._pid_alive("not-an-int") is False  # type: ignore[arg-type]


# ---------- direct reconciliation of frame dicts ----------


def test_reconcile_orphan_promotes_non_terminal_to_abandoned(tmp_path):
    """The core fix: a frame whose writer_pid is dead and whose nodes are
    still in awaiting_* gets its node kinds rewritten to 'abandoned' so
    the merged report's status is no longer pinned at 'running'."""
    ac._frames_cache_clear()
    report = InvocationReport(invoke_id="inv-promote", log_dir=tmp_path / "log", cwd="/cwd")

    parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
    stuck = _running_node("aaaaaaaa", "stuck task")
    tree = Tree(root_session=parent, nodes=[stuck], base_depth=0)
    _write_frame(report, frame_key=ROOT_FRAME_KEY, tree=tree, writer_pid=_dead_pid())

    frames = report.load_frames()
    root_frame = frames[ROOT_FRAME_KEY][0]
    [node] = root_frame["tree"]["nodes"]
    assert node["state"]["kind"] == "abandoned", (
        "non-terminal node from a dead-writer frame should be promoted to "
        "the synthetic 'abandoned' kind so unwind no longer renders it as "
        "in-progress"
    )
    assert node.get("error"), (
        "abandoned node must carry an error message so the merged report explains *why* it stopped advancing"
    )


def test_reconcile_does_not_touch_live_writer_frames(tmp_path):
    """Defensive: a frame written by *this* very process must NEVER be
    reconciled — otherwise every live invocation would have its own
    in-flight nodes marked abandoned on the next merge tick."""
    ac._frames_cache_clear()
    report = InvocationReport(invoke_id="inv-live", log_dir=tmp_path / "log", cwd="/cwd")

    parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
    running = _running_node("bbbbbbbb", "in flight task")
    tree = Tree(root_session=parent, nodes=[running], base_depth=0)
    _write_frame(report, frame_key=ROOT_FRAME_KEY, tree=tree, writer_pid=os.getpid())

    frames = report.load_frames()
    [node] = frames[ROOT_FRAME_KEY][0]["tree"]["nodes"]
    assert node["state"]["kind"] == "awaiting_turn", (
        "live-writer frames must stay untouched — otherwise the reporter "
        "would mark its own in-flight nodes as abandoned on the next tick"
    )


def test_reconcile_skips_frames_without_writer_pid(tmp_path):
    """Older frame files (and externally-produced ones) don't have a
    writer_pid field. We can't decide liveness, so we leave them alone
    — better to keep showing 'running' than to falsely abandon a
    live nested invocation."""
    ac._frames_cache_clear()
    report = InvocationReport(invoke_id="inv-nopid", log_dir=tmp_path / "log", cwd="/cwd")

    parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
    running = _running_node("cccccccc", "legacy frame task")
    tree = Tree(root_session=parent, nodes=[running], base_depth=0)
    _write_frame(report, frame_key=ROOT_FRAME_KEY, tree=tree, writer_pid=None)

    frames = report.load_frames()
    [node] = frames[ROOT_FRAME_KEY][0]["tree"]["nodes"]
    assert node["state"]["kind"] == "awaiting_turn"


def test_reconcile_leaves_terminal_nodes_alone(tmp_path):
    """A completed nested invocation has all-terminal nodes by the time
    its driver exits. Its pid then dies. The reconciler must NOT touch
    those nodes — they're already terminal — otherwise it would clobber
    real `Done`/`Failed` results with synthetic 'abandoned'."""
    ac._frames_cache_clear()
    report = InvocationReport(invoke_id="inv-terminal", log_dir=tmp_path / "log", cwd="/cwd")

    parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
    done_node = Node(
        id="ddddeeee",
        task="completed",
        state=st.Done(session_id="sess-done", result="ok"),
    )
    tree = Tree(root_session=parent, nodes=[done_node], base_depth=0)
    _write_frame(report, frame_key=ROOT_FRAME_KEY, tree=tree, writer_pid=_dead_pid())

    frames = report.load_frames()
    [node] = frames[ROOT_FRAME_KEY][0]["tree"]["nodes"]
    assert node["state"]["kind"] == "done"
    assert node["result"] == "ok"


def test_reconcile_recurses_into_children(tmp_path):
    """Stuck states can live arbitrarily deep in the tree — the
    reconciler walks the full subtree, not just root nodes."""
    ac._frames_cache_clear()
    report = InvocationReport(invoke_id="inv-recurse", log_dir=tmp_path / "log", cwd="/cwd")

    parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
    stuck_grandchild = _running_node("gggggggg", "deep stuck")
    stuck_child = _running_node("ffffffff", "mid stuck")
    stuck_child.children = [stuck_grandchild]
    root = _running_node("eeeeeeee", "top stuck")
    root.children = [stuck_child]
    tree = Tree(root_session=parent, nodes=[root], base_depth=0)
    _write_frame(report, frame_key=ROOT_FRAME_KEY, tree=tree, writer_pid=_dead_pid())

    frames = report.load_frames()
    [r] = frames[ROOT_FRAME_KEY][0]["tree"]["nodes"]
    assert r["state"]["kind"] == "abandoned"
    [c] = r["children"]
    assert c["state"]["kind"] == "abandoned"
    [g] = c["children"]
    assert g["state"]["kind"] == "abandoned"


# ---------- merged report consequences ----------


def test_merged_report_status_settles_to_abandoned(tmp_path):
    """End-to-end: a single-task report whose only node is abandoned
    surfaces overall status='abandoned' (not 'running' or 'mixed'),
    which is what unwind needs to stop rendering the spinner."""
    ac._frames_cache_clear()
    report = InvocationReport(invoke_id="inv-orphan", log_dir=tmp_path / "log", cwd=str(tmp_path))

    parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
    stuck = _running_node("aaaa1111", "killed mid-turn")
    tree = Tree(root_session=parent, nodes=[stuck], base_depth=0)
    _write_frame(report, frame_key=ROOT_FRAME_KEY, tree=tree, writer_pid=_dead_pid())

    doc = report.merged_document(ended_at="2026-05-22T00:00:00+00:00")
    assert doc["status"] == "abandoned"
    assert doc["tasks"][0]["status"] == "abandoned"
    assert doc["tasks"][0]["error"], (
        "merged report's abandoned task must surface the error message so "
        "the UI can explain *why* it stopped, not just *that* it stopped"
    )


def test_reconcile_preserves_awaiting_user_nodes(tmp_path, monkeypatch):
    """REVIEW-201 bug fix: the dict-shape walker used to consult only
    `_TERMINAL_KINDS` and would happily rewrite an `awaiting_user` (a
    legitimately YIELDED node parked for a user reply) to `abandoned`,
    silently destroying the yield intent. The unified policy now skips
    SUSPENDED kinds — confirm an AwaitingUser frame whose writer is dead
    stays awaiting_user instead of getting demoted."""
    ac._frames_cache_clear()
    report = InvocationReport(invoke_id="inv-awaiting-user", log_dir=tmp_path / "log", cwd="/cwd")

    parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
    yielded = Node(
        id="yyyyyyyy",
        task="ask user",
        state=st.AwaitingUser(session_id="sess-yielded", question="ok?"),
    )
    tree = Tree(root_session=parent, nodes=[yielded], base_depth=0)

    _write_frame(report, frame_key=ROOT_FRAME_KEY, tree=tree, writer_pid=99999)  # nonexistent — guaranteed dead
    monkeypatch.setattr(frames_mod, "_pid_alive", lambda _pid: False)

    loaded = report.load_frames()
    [node] = loaded[ROOT_FRAME_KEY][0]["tree"]["nodes"]
    assert node["state"]["kind"] == "awaiting_user", (
        "AwaitingUser nodes are SUSPENDED (parked for a user reply), not "
        "non-terminal-and-stuck. The abandonment policy must skip them."
    )


def test_status_label_maps_abandoned_kind():
    """The new synthetic kind must be in `_STATUS_BY_KIND` so callers of
    `status_label` (driver, frames merge) get a real label rather than
    the fallback 'unknown'."""
    assert st.status_label({"kind": "abandoned"}) == "abandoned"


def test_reconcile_runs_on_dir_mtime_cache_hit(tmp_path, monkeypatch):
    """The PERF-104 dir-mtime fast-path returns a cached snapshot when
    the frames dir hasn't been touched. Reconciliation must still run
    on those cache hits — a writer can die between the cache prime and
    a subsequent read, and the report must converge on the next tick."""
    ac._frames_cache_clear()
    report = InvocationReport(invoke_id="inv-cachehit", log_dir=tmp_path / "log", cwd="/cwd")

    parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
    running = _running_node("hhhhhhhh", "to be abandoned")
    tree = Tree(root_session=parent, nodes=[running], base_depth=0)

    # Stamp the frame with a pid that's still alive (this process) so the
    # first load primes the cache without reconciling.
    _write_frame(report, frame_key=ROOT_FRAME_KEY, tree=tree, writer_pid=os.getpid())

    first = report.load_frames()
    [node] = first[ROOT_FRAME_KEY][0]["tree"]["nodes"]
    assert node["state"]["kind"] == "awaiting_turn", "pre-condition: live-writer frame must NOT be reconciled"

    # Now flip liveness without touching the file (so the dir-mtime cache
    # hit path is exercised) and verify the next load reconciles.
    monkeypatch.setattr(frames_mod, "_pid_alive", lambda _pid: False)
    second = report.load_frames()
    [node2] = second[ROOT_FRAME_KEY][0]["tree"]["nodes"]
    assert node2["state"]["kind"] == "abandoned", (
        "dir-mtime cache hit must still run reconciliation — otherwise a "
        "writer that dies after the cache prime stays 'running' forever "
        "until something else bumps the dir's mtime"
    )


def test_ttl_fallback_treats_old_frame_as_abandoned(tmp_path, monkeypatch):
    """REVIEW-204: even when `_pid_alive` returns True, a frame whose
    wall-clock age exceeds the orphan TTL must be reconciled. Defends
    against PID reuse — a dead writer's pid getting recycled by an
    unrelated long-running process would otherwise keep its frame
    "alive-looking" forever.

    Monkey-patch `_pid_alive` to True (simulating the PID-reuse hit) and
    set a tiny TTL so a freshly-written frame trips the wall-clock
    branch. Reconciliation must still fire."""
    ac._frames_cache_clear()
    report = InvocationReport(invoke_id="inv-ttl", log_dir=tmp_path / "log", cwd="/cwd")

    parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
    tree = Tree(root_session=parent, nodes=[_running_node("tttttttt", "stuck")], base_depth=0)
    # Frame from "an hour ago" — `started_at` is far older than the TTL
    # we'll configure below.
    frame = {
        "frame_key": ROOT_FRAME_KEY,
        "is_nested": False,
        "kind": "call",
        "tasks": ["t"],
        "cwd": "/cwd",
        "started_at": "2020-01-01T00:00:00+00:00",
        "ended_at": "e",
        "tree": tree.to_dict(),
        "writer_pid": os.getpid(),
    }
    report.write_frame(frame, key=ROOT_FRAME_KEY)

    monkeypatch.setattr(frames_mod, "_pid_alive", lambda _pid: True)
    monkeypatch.setenv("CALLSTACK_ORPHAN_TTL_SECONDS", "1")

    loaded = report.load_frames()
    [node] = loaded[ROOT_FRAME_KEY][0]["tree"]["nodes"]
    assert node["state"]["kind"] == "abandoned", (
        "TTL fallback should mark the frame abandoned even when pid is "
        "(falsely) reported alive — defense against PID reuse"
    )


def test_ttl_fallback_disabled_with_zero(tmp_path, monkeypatch):
    """Setting CALLSTACK_ORPHAN_TTL_SECONDS=0 opts out of the TTL check
    entirely so old frames are kept "running" as long as `_pid_alive`
    says so. Regression guard that the opt-out actually works."""
    ac._frames_cache_clear()
    report = InvocationReport(invoke_id="inv-ttl-zero", log_dir=tmp_path / "log", cwd="/cwd")
    parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
    tree = Tree(root_session=parent, nodes=[_running_node("zzzzzzzz", "ancient")], base_depth=0)
    frame = {
        "frame_key": ROOT_FRAME_KEY,
        "is_nested": False,
        "kind": "call",
        "tasks": ["t"],
        "cwd": "/cwd",
        "started_at": "2020-01-01T00:00:00+00:00",
        "ended_at": "e",
        "tree": tree.to_dict(),
        "writer_pid": os.getpid(),
    }
    report.write_frame(frame, key=ROOT_FRAME_KEY)

    monkeypatch.setattr(frames_mod, "_pid_alive", lambda _pid: True)
    monkeypatch.setenv("CALLSTACK_ORPHAN_TTL_SECONDS", "0")

    loaded = report.load_frames()
    [node] = loaded[ROOT_FRAME_KEY][0]["tree"]["nodes"]
    assert node["state"]["kind"] == "awaiting_turn", (
        "TTL=0 must restore pre-fix behavior: live pid keeps the frame running regardless of age"
    )


def test_cache_returns_independent_copies(tmp_path):
    """`load_frames` promises callers can mutate the returned dict and
    nested frame contents freely. The internal dir-mtime + parsed-frame
    caches must deep-copy on retrieval so a caller-side mutation cannot
    poison subsequent loads. Regression for REVIEW-205: the in-flight
    diff shared frame-dict references between the cache and callers."""
    ac._frames_cache_clear()
    report = InvocationReport(invoke_id="inv-indep", log_dir=tmp_path / "log", cwd="/cwd")
    parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
    tree = Tree(root_session=parent, nodes=[_running_node("nnnnnnnn", "t")], base_depth=0)
    # Stamp with this process's pid so reconciliation is a no-op and
    # any visible mutation must have come from the caller side.
    _write_frame(report, frame_key=ROOT_FRAME_KEY, tree=tree, writer_pid=os.getpid())

    first = report.load_frames()
    # Aggressive caller mutation: rewrite the inner frame dict.
    first[ROOT_FRAME_KEY][0]["tree"]["nodes"][0]["task"] = "POISONED"
    first[ROOT_FRAME_KEY][0]["frame_key"] = "POISONED_KEY"
    first["INJECTED_KEY"] = []

    # Second load must NOT see any of the caller-side mutations — both
    # the dir-mtime cache hit and the per-file parsed cache hit are on
    # the hot path here (mtime hasn't changed).
    second = report.load_frames()
    assert "INJECTED_KEY" not in second
    assert second[ROOT_FRAME_KEY][0]["frame_key"] == ROOT_FRAME_KEY
    assert second[ROOT_FRAME_KEY][0]["tree"]["nodes"][0]["task"] == "t"


def test_writer_pid_is_stamped_on_live_frame_writes(tmp_path):
    """Sanity-check the producer side: the live reporter's frame write must
    record the current pid so the reconciler has something to probe."""
    report = InvocationReport(invoke_id="inv-pid-stamp", log_dir=tmp_path / "log", cwd=str(tmp_path))
    report.invocation_dir.mkdir(parents=True, exist_ok=True)
    report.frames_dir.mkdir(parents=True, exist_ok=True)
    reporter = report.reporter(kind="call", tasks=["t"], started_at="s")
    parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
    node = _running_node("iiiiiiii", "t")
    tree = Tree(root_session=parent, nodes=[node], base_depth=0)
    reporter._write_frame(tree, ended_at="e")

    written = yaml.safe_load(report.frame_path().read_text())
    assert written.get("writer_pid") == os.getpid()


# ---------- reconcile_orphans: pure, FakeLiveness-driven (no monkeypatching) ----------


class FakeLiveness:
    """In-memory Liveness: a controlled set of live pids and a fixed clock.
    Lets reconcile_orphans be exercised with no real PIDs and no monkeypatching
    of frames._pid_alive."""

    def __init__(self, *, alive: set[int], clock: float = 1000.0):
        self._alive = alive
        self._clock = clock
        self.now_calls = 0

    def pid_alive(self, pid: int) -> bool:
        return pid in self._alive

    def now(self) -> float:
        self.now_calls += 1
        return self._clock


def _frame(pid: int, *, kind: str = "awaiting_turn", started_at: str | None = None) -> dict:
    f: dict = {"writer_pid": pid, "tree": {"nodes": [{"id": "n1", "state": {"kind": kind, "session_id": "s1"}}]}}
    if started_at is not None:
        f["started_at"] = started_at
    return f


def test_reconcile_orphans_seals_dead_writer():
    from agent_callstack.frames import reconcile_orphans
    from agent_callstack.liveness import OrphanPolicy

    frames = {"root": [_frame(4242)]}
    sealed = reconcile_orphans(frames, liveness=FakeLiveness(alive=set()), policy=OrphanPolicy(ttl_seconds=0))
    assert sealed == 1
    assert frames["root"][0]["tree"]["nodes"][0]["state"]["kind"] == "abandoned"
    assert frames["root"][0]["tree"]["nodes"][0]["state"]["session_id"] == "s1"


def test_reconcile_orphans_live_writer_is_noop():
    from agent_callstack.frames import reconcile_orphans
    from agent_callstack.liveness import OrphanPolicy

    frames = {"root": [_frame(7)]}
    sealed = reconcile_orphans(frames, liveness=FakeLiveness(alive={7}), policy=OrphanPolicy(ttl_seconds=0))
    assert sealed == 0
    assert frames["root"][0]["tree"]["nodes"][0]["state"]["kind"] == "awaiting_turn"


def test_reconcile_orphans_ttl_defeats_pid_reuse():
    # pid reads ALIVE (reuse), but the frame is ancient -> TTL declares it dead.
    from agent_callstack.frames import reconcile_orphans
    from agent_callstack.liveness import OrphanPolicy

    frames = {"root": [_frame(7, started_at="1970-01-01T00:00:00Z")]}
    live = FakeLiveness(alive={7}, clock=10_000.0)
    sealed = reconcile_orphans(frames, liveness=live, policy=OrphanPolicy(ttl_seconds=60))
    assert sealed == 1
    assert frames["root"][0]["tree"]["nodes"][0]["state"]["kind"] == "abandoned"


def test_reconcile_orphans_ttl_zero_keeps_old_live_frame():
    # ttl_seconds=0 opts out of the TTL fallback: an ancient but pid-alive frame
    # stays running.
    from agent_callstack.frames import reconcile_orphans
    from agent_callstack.liveness import OrphanPolicy

    frames = {"root": [_frame(7, started_at="1970-01-01T00:00:00Z")]}
    live = FakeLiveness(alive={7}, clock=10_000.0)
    sealed = reconcile_orphans(frames, liveness=live, policy=OrphanPolicy(ttl_seconds=0))
    assert sealed == 0


def test_reconcile_orphans_reads_clock_once():
    # A slow walk must see a stable `now`: liveness.now() is called exactly once
    # regardless of frame count.
    from agent_callstack.frames import reconcile_orphans
    from agent_callstack.liveness import OrphanPolicy

    frames = {"root": [_frame(1), _frame(2)], "k": [_frame(3, started_at="1970-01-01T00:00:00Z")]}
    live = FakeLiveness(alive={1, 2, 3}, clock=10_000.0)
    reconcile_orphans(frames, liveness=live, policy=OrphanPolicy(ttl_seconds=60))
    assert live.now_calls == 1


def test_reconcile_orphans_frame_without_pid_skipped():
    from agent_callstack.frames import reconcile_orphans
    from agent_callstack.liveness import OrphanPolicy

    frames = {"root": [{"tree": {"nodes": [{"id": "n", "state": {"kind": "awaiting_turn"}}]}}]}
    sealed = reconcile_orphans(frames, liveness=FakeLiveness(alive=set()), policy=OrphanPolicy(ttl_seconds=0))
    assert sealed == 0


def test_os_liveness_probe_matches_reality():
    from agent_callstack.liveness import OsLiveness

    live = OsLiveness()
    assert live.pid_alive(os.getpid()) is True
    assert live.pid_alive(0) is False
    assert live.pid_alive(-1) is False
