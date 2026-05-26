"""Tests for the per-invocation YAML report writer."""
from __future__ import annotations

import json

import yaml

from agent_callstack import InvocationReport, ROOT_FRAME_KEY
from tests._helpers import write_invocation_report as _write_invocation_report
from agent_callstack.channel import ScriptedChannel
from agent_callstack.driver import Driver, Node, Tree
from agent_callstack.session import SessionLocator, SessionRef
from agent_callstack.trace import TraceWriter, TreeStore
from agent_callstack import state as st

# ---- White-box internals (PERF-* optimization tests only) ----
# The optimization tests in the PERF-A section below assert internal
# write/parse behavior — debounce coalescing, content-hash write-skip,
# parsed-frame LRU cache, dir-mtime glob-skip — that has no honest
# expression through the public `InvocationReport` boundary. They build
# the runtime internals directly on purpose; do NOT route them through
# the facade. Behavioral tests in this file use `InvocationReport`.
from agent_callstack.invocation_ctx import _InvocationContext
from agent_callstack.reporter import _LiveReporter


def _done_node(nid: str, task: str, result: str, *, children=None,
               summary: str | None = None, duration: float = 0.0,
               session_id: str | None = None) -> Node:
    sid = session_id or f"sess-{nid}"
    return Node(
        id=nid, task=task,
        state=st.Done(session_id=sid, result=result, summary=summary),
        session_id=sid, result=result, summary=summary,
        duration=duration, children=list(children or []),
    )


# ---------- one-shot writer ----------

def test_report_serializes_single_root(tmp_path):
    parent = SessionRef(session_id="parent", file=tmp_path / "parent.jsonl")
    root = _done_node("abcdef12", "top task", "final answer",
                      summary="did the thing", duration=4.2)
    tree = Tree(root_session=parent, nodes=[root], base_depth=0)

    path = _write_invocation_report(
        log_dir=tmp_path / "log", invoke_id="inv1", kind="call",
        tasks=["top task"], tree=tree, cwd="/some/cwd",
        started_at="2026-04-19T14:00:00+00:00",
        ended_at="2026-04-19T14:00:05+00:00",
    )

    assert path == tmp_path / "log" / "inv1" / "report.yaml"
    doc = yaml.safe_load(path.read_text())
    assert doc["invoke_id"] == "inv1"
    assert doc["kind"] == "call"
    assert doc["status"] == "complete"
    assert doc["cwd"] == "/some/cwd"
    assert doc["parent_session"] == "parent"
    t = doc["tasks"][0]
    assert t["task"] == "top task"
    assert t["input"] == "top task"
    assert t["output"] == "final answer"
    assert t["summary"] == "did the thing"
    assert t["status"] == "complete"
    assert t["depth"] == 1


def test_report_nests_children(tmp_path):
    parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
    child = _done_node("child001", "sub task", "sub result")
    root = _done_node("root0001", "root task", "root result", children=[child])
    tree = Tree(root_session=parent, nodes=[root], base_depth=0)

    path = _write_invocation_report(
        log_dir=tmp_path / "log", invoke_id="inv2", kind="call",
        tasks=["root task"], tree=tree, cwd="/cwd",
        started_at="s", ended_at="e",
    )

    doc = yaml.safe_load(path.read_text())
    c = doc["tasks"][0]["children"][0]
    assert c["task"] == "sub task"
    assert c["output"] == "sub result"
    assert c["depth"] == 2


def test_report_overall_status_reflects_leaves(tmp_path):
    parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
    ok = _done_node("ok000000", "a", "a-done")
    failed = Node(
        id="ff000000", task="b",
        state=st.Failed(error="nope"), error="nope",
    )
    tree = Tree(root_session=parent, nodes=[ok, failed], base_depth=0)

    path = _write_invocation_report(
        log_dir=tmp_path / "log", invoke_id="inv3", kind="call",
        tasks=["a", "b"], tree=tree, cwd="/cwd",
        started_at="s", ended_at="e",
    )
    doc = yaml.safe_load(path.read_text())
    assert doc["status"] == "mixed"
    assert doc["tasks"][0]["status"] == "complete"
    assert doc["tasks"][1]["status"] == "error"
    assert doc["tasks"][1]["error"] == "nope"


def test_report_writes_yaml_at_expected_path(tmp_path):
    parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
    root = _done_node("r0000001", "x", "y")
    tree = Tree(root_session=parent, nodes=[root], base_depth=0)

    log_dir = tmp_path / "deep" / "nested" / "log"
    assert not log_dir.exists()
    path = _write_invocation_report(
        log_dir=log_dir, invoke_id="inv4", kind="call",
        tasks=["x"], tree=tree, cwd="/cwd",
        started_at="s", ended_at="e",
    )
    assert path.exists()
    assert path == log_dir / "inv4" / "report.yaml"


# ---------- live reporter ----------

def test_driver_progress_callback_fires_per_transition(tmp_path, monkeypatch):
    # This test asserts the legacy per-notify report.yaml write contract.
    # PERF-A added a default 250 ms debounce; opt into synchronous-merge
    # mode here so intermediate snapshots land before the run finishes.
    monkeypatch.setenv("CALLSTACK_REPORT_DEBOUNCE_SECS", "0")
    parent_file = tmp_path / "parent.jsonl"
    parent_file.write_text(json.dumps({"cwd": str(tmp_path), "type": "user"}) + "\n")
    parent = SessionRef(session_id="parent", file=parent_file)

    project = tmp_path / "projects" / "fake"
    project.mkdir(parents=True)
    (project / "child.jsonl").write_text("")

    envelope = "```json\n" + json.dumps({"op": "return", "result": "done"}) + "\n```"
    channel = ScriptedChannel().respond(envelope, "child")

    log_dir = tmp_path / "log"
    snapshots: list[str] = []

    report = InvocationReport(
        invoke_id="inv-live", log_dir=log_dir, cwd=str(tmp_path),
    )
    driver = Driver(
        channel=channel,
        resolve_session=SessionLocator(projects_dir=tmp_path / "projects").resolve,
        trace=TraceWriter(report.invocation_dir),
        store=TreeStore(),
        cwd=str(tmp_path), timeout=10, max_depth=5,
    )
    reporter = report.reporter(kind="call", tasks=["do it"], started_at="s")

    def spy(tree):
        reporter(tree)
        snapshots.append(report.report_path.read_text())

    driver.on_progress = spy
    tree = driver.run(parent, ["do it"])
    reporter.finalize(tree)

    assert len(snapshots) >= 2
    first = yaml.safe_load(snapshots[0])
    last = yaml.safe_load(report.report_path.read_text())
    assert first["tasks"][0]["status"] == "pending"
    assert last["tasks"][0]["status"] == "complete"
    assert last["tasks"][0]["output"] == "done"

    # Append-only log (tail-friendly) sits inside the invocation dir.
    log_lines = [ln for ln in report.log_path.read_text().splitlines() if ln]
    pending_at = next(i for i, ln in enumerate(log_lines) if " pending " in ln)
    complete_at = next(i for i, ln in enumerate(log_lines) if " complete " in ln)
    assert pending_at < complete_at
    assert 'result="done"' in log_lines[complete_at]

    # No stray sidecar files at the log_dir root — everything under {invoke_id}/.
    top_entries = {p.name for p in log_dir.iterdir()}
    assert top_entries == {"inv-live"}


# ---------- nested frame merging ----------

def test_merged_report_grafts_nested_frame_under_calling_node(tmp_path):
    """Simulate two cooperating reporters: root writes tree with node whose
    session is `sess-C`; a nested reporter with frame_key=`sess-C` writes
    a tree with nodes E and F. The merged report should show E and F as
    children of the root's C node."""
    log_dir = tmp_path / "log"

    # Root invocation: B, C, D — C has session_id "sess-C"
    parent = SessionRef(session_id="top", file=tmp_path / "top.jsonl")
    b = _done_node("b0000000", "/task-b", "B done", session_id="sess-B")
    c = _done_node("c0000000", "/task-c", "C done", session_id="sess-C")
    d = _done_node("d0000000", "/task-d", "D done", session_id="sess-D")
    root_tree = Tree(root_session=parent, nodes=[b, c, d], base_depth=0)

    root_report = InvocationReport(
        invoke_id="inv-merge", log_dir=log_dir, cwd="/cwd",
    )
    root_reporter = root_report.reporter(kind="call",
                                         tasks=["/task-b", "/task-c", "/task-d"],
                                         started_at="s")

    # Nested invocation (from inside C's subprocess): E, F
    e = _done_node("e0000000", "/task-e", "E done", session_id="sess-E")
    f = _done_node("f0000000", "/task-f", "F done", session_id="sess-F")
    nested_parent = SessionRef(session_id="sess-C", file=tmp_path / "c.jsonl")
    nested_tree = Tree(root_session=nested_parent, nodes=[e, f], base_depth=1)

    nested_report = InvocationReport(
        invoke_id="inv-merge", log_dir=log_dir, cwd="/cwd",
        frame_key="sess-C",  # the calling session
        is_nested=True,
    )
    nested_reporter = nested_report.reporter(kind="nested_call",
                                             tasks=["/task-e", "/task-f"],
                                             started_at="s")

    # Realistic ordering: root writes at least one snapshot (so root.yaml
    # exists with C), nested fires during C's turn, root finalizes.
    root_reporter(root_tree)
    nested_reporter.finalize(nested_tree)
    root_reporter.finalize(root_tree)

    doc = yaml.safe_load(root_report.report_path.read_text())
    assert doc["invoke_id"] == "inv-merge"
    assert doc["nested_frames"] == ["sess-C"]
    task_by_sess = {t["session_id"]: t for t in doc["tasks"]}
    c_task = task_by_sess["sess-C"]
    child_sessions = {c["session_id"] for c in c_task.get("children", [])}
    assert child_sessions == {"sess-E", "sess-F"}
    # E and F inherit the correct depth from C's depth (1) + 1.
    for child in c_task["children"]:
        assert child["depth"] == 2

    # B and D unaffected.
    assert not task_by_sess["sess-B"].get("children")
    assert not task_by_sess["sess-D"].get("children")

    # Shared log: nested lines carry the caller-node id in the chain,
    # e.g. [c0000000→e0000000]. The caller id is resolved from the root
    # frame by matching session_id.
    log_text = root_report.log_path.read_text()
    assert "[c0000000→e0000000]" in log_text
    assert "[c0000000→f0000000]" in log_text
    # Root-level B/C/D lines have no ancestor prefix.
    assert "[b0000000]" in log_text
    assert "[c0000000]" in log_text
    assert "[d0000000]" in log_text


def test_encode_project_dir_matches_claude_code(tmp_path):
    """Claude Code replaces both `/` and `_` with `-` when mapping a cwd
    into `~/.claude/projects/<slug>/`. Getting the encoding wrong means
    the mtime fallback can't find the active session file."""
    from agent_callstack.session import encode_project_dir
    assert encode_project_dir("/a/b/c") == "-a-b-c"
    assert encode_project_dir("/a/parallel_calls") == "-a-parallel-calls"
    assert encode_project_dir("/a_b/c_d") == "-a-b-c-d"


def test_three_level_chain_prefix(tmp_path):
    """Depth-3 nesting (G under E under C): G's log lines must show the
    full ancestor chain `[c_short→e_short→g_short]`, which requires the
    chain lookup to walk beyond root.yaml into nested-frame sidecars."""
    log_dir = tmp_path / "log"
    parent = SessionRef(session_id="top", file=tmp_path / "top.jsonl")
    c = _done_node("C0000000", "/task-c", "C done", session_id="sess-C")
    root_tree = Tree(root_session=parent, nodes=[c], base_depth=0)
    root_report = InvocationReport(
        invoke_id="inv-3lvl", log_dir=log_dir, cwd="/cwd",
    )
    root_reporter = root_report.reporter(kind="call",
                                         tasks=["/task-c"], started_at="s")

    # Level-2 frame: E (caller = C)
    e = _done_node("E0000000", "/task-e", "E done", session_id="sess-E")
    e_parent = SessionRef(session_id="sess-C", file=tmp_path / "c.jsonl")
    e_tree = Tree(root_session=e_parent, nodes=[e], base_depth=1)
    e_report = InvocationReport(
        invoke_id="inv-3lvl", log_dir=log_dir, cwd="/cwd",
        frame_key=c.id, is_nested=True,
    )
    e_reporter = e_report.reporter(kind="nested_call",
                                   tasks=["/task-e"], started_at="s")

    # Level-3 frame: G (caller = E)
    g = _done_node("G0000000", "/task-g", "G done", session_id="sess-G")
    g_parent = SessionRef(session_id="sess-E", file=tmp_path / "e.jsonl")
    g_tree = Tree(root_session=g_parent, nodes=[g], base_depth=2)
    g_report = InvocationReport(
        invoke_id="inv-3lvl", log_dir=log_dir, cwd="/cwd",
        frame_key=e.id, is_nested=True,
    )
    g_reporter = g_report.reporter(kind="nested_call",
                                   tasks=["/task-g"], started_at="s")

    # Order matters for chain lookups: root and E must exist on disk
    # before G looks up E's ancestry.
    root_reporter(root_tree)
    e_reporter(e_tree)
    g_reporter.finalize(g_tree)
    e_reporter.finalize(e_tree)
    root_reporter.finalize(root_tree)

    log_text = root_report.log_path.read_text()
    assert "[C0000000→E0000000→G0000000]" in log_text, (
        f"missing 3-level chain in log:\n{log_text}"
    )
    # Also check the grafted report: G nested under E nested under C.
    doc = yaml.safe_load(root_report.report_path.read_text())
    c_task = doc["tasks"][0]
    assert c_task["children"][0]["session_id"] == "sess-E"
    assert c_task["children"][0]["children"][0]["session_id"] == "sess-G"


def test_merge_by_node_id_wins_over_sessions(tmp_path):
    """Primary path: parent Driver stamps each forked subprocess with
    `CALLSTACK_FRAME_KEY=<node.id>`, nested Caller uses that as its frame
    key, and the merger matches on node id — regardless of whether sibling
    sessions happen to have newer mtimes. No session-id dependency."""
    log_dir = tmp_path / "log"
    parent = SessionRef(session_id="top", file=tmp_path / "top.jsonl")
    # Three parallel siblings, all with distinct session ids.
    b = _done_node("bNODE000", "/task-b", "B done", session_id="sess-B")
    c = _done_node("cNODE000", "/task-c", "C done", session_id="sess-C")
    d = _done_node("dNODE000", "/task-d", "D done", session_id="sess-D")
    root_tree = Tree(root_session=parent, nodes=[b, c, d], base_depth=0)

    root_report = InvocationReport(
        invoke_id="inv-by-nodeid", log_dir=log_dir, cwd="/cwd",
    )
    root_reporter = root_report.reporter(kind="call",
                                         tasks=["/task-b", "/task-c", "/task-d"],
                                         started_at="s")

    e = _done_node("eNODE000", "/task-e", "E done", session_id="sess-E")
    f = _done_node("fNODE000", "/task-f", "F done", session_id="sess-F")
    nested_parent = SessionRef(session_id="sess-C", file=tmp_path / "c.jsonl")
    nested_tree = Tree(root_session=nested_parent, nodes=[e, f], base_depth=1)

    # Nested frame keyed by C's full node id (what CALLSTACK_FRAME_KEY holds).
    nested_report = InvocationReport(
        invoke_id="inv-by-nodeid", log_dir=log_dir, cwd="/cwd",
        frame_key=c.id,
        is_nested=True,
    )
    nested_reporter = nested_report.reporter(kind="nested_call",
                                             tasks=["/task-e", "/task-f"],
                                             started_at="s")

    root_reporter(root_tree)
    nested_reporter.finalize(nested_tree)
    root_reporter.finalize(root_tree)

    doc = yaml.safe_load(root_report.report_path.read_text())
    c_task = next(t for t in doc["tasks"] if t["session_id"] == "sess-C")
    assert {child["session_id"] for child in c_task["children"]} == {"sess-E", "sess-F"}
    # Log chain prefixed with C's short id.
    log_text = root_report.log_path.read_text()
    assert "[cNODE000→eNODE000]" in log_text
    assert "[cNODE000→fNODE000]" in log_text


def test_nested_detection_uses_mtime_fallback(tmp_path, monkeypatch):
    """When CLAUDE_SESSION_ID is absent but CALLSTACK_ROOT_* are set, the
    Caller must still identify the calling session via the most recently
    modified .jsonl in the project dir, so frame_key is non-'root'."""
    from agent_callstack import (
        Caller, ENV_ROOT_INVOKE_ID, ENV_ROOT_LOG_DIR, ENV_CLAUDE_SESSION,
    )
    # Fake projects dir for this cwd; seed with a plausibly-active session.
    fake_claude = tmp_path / "fake-home" / ".claude"
    projects = fake_claude / "projects"
    cwd = tmp_path / "workdir"
    cwd.mkdir()
    from agent_callstack.session import encode_project_dir
    proj = projects / encode_project_dir(str(cwd))
    proj.mkdir(parents=True)
    # Two sessions; the newer one should win the mtime race.
    (proj / "older.jsonl").write_text(json.dumps({"cwd": str(cwd)}) + "\n")
    import os as _os, time as _time
    _time.sleep(0.01)
    newest = proj / "newer.jsonl"
    newest.write_text(json.dumps({"cwd": str(cwd)}) + "\n")
    # Ensure newer has strictly greater mtime.
    _os.utime(newest, (newest.stat().st_atime, newest.stat().st_mtime + 1))

    # `_most_recent_session` (now in agent_callstack.frames) reads from
    # `agent_callstack.session.PROJECTS_DIR` dynamically — point both there
    # and at the legacy re-export so older monkeypatches keep working.
    monkeypatch.setattr("agent_callstack.session.PROJECTS_DIR", projects)
    monkeypatch.setattr("agent_callstack.PROJECTS_DIR", projects)
    monkeypatch.setenv(ENV_ROOT_INVOKE_ID, "inv-nest-fallback")
    monkeypatch.setenv(ENV_ROOT_LOG_DIR, str(tmp_path / "log"))
    monkeypatch.delenv(ENV_CLAUDE_SESSION, raising=False)
    # If we're running under a callstack-forked test process, our own
    # CALLSTACK_FRAME_KEY would otherwise short-circuit the mtime fallback
    # this test exists to exercise.
    monkeypatch.delenv("CALLSTACK_FRAME_KEY", raising=False)

    caller = Caller(cwd=str(cwd))
    parent = SessionRef(session_id="parent", file=proj / "older.jsonl")
    ctx = caller._resolve_invocation_context(parent)
    assert ctx.is_nested is True
    assert ctx.frame_key == "newer"
    assert ctx.invoke_id == "inv-nest-fallback"


def test_sibling_nested_invocations_dont_overwrite_each_others_frame(tmp_path):
    """A forked session that issues several sibling ``invoke*`` calls (e.g.
    deep-rewrite running specialists, then meta-assessors, then a re-author)
    used to lose all but the last invocation's children, because every
    nested reporter wrote to ``_frames/{frame_key}.yaml`` and the next call
    overwrote the previous frame. With per-invocation ``instance_id`` in the
    filename and list-valued frame grouping, all sibling invocations'
    children survive in the merged report."""
    log_dir = tmp_path / "log"
    parent = SessionRef(session_id="top", file=tmp_path / "top.jsonl")
    c = _done_node("c0000000", "/task-c", "C done", session_id="sess-C")
    root_tree = Tree(root_session=parent, nodes=[c], base_depth=0)

    root_report = InvocationReport(
        invoke_id="inv-siblings", log_dir=log_dir, cwd="/cwd",
    )
    root_reporter = root_report.reporter(kind="call",
                                         tasks=["/task-c"], started_at="s0")

    # Two sibling nested invocations from inside C, each with its own
    # children. Same frame_key (C's id), distinct instance_ids → distinct
    # frame files.
    e = _done_node("e0000000", "/task-e", "E done", session_id="sess-E")
    e_parent = SessionRef(session_id="sess-C", file=tmp_path / "c.jsonl")
    e_tree = Tree(root_session=e_parent, nodes=[e], base_depth=1)
    e_report = InvocationReport(
        invoke_id="inv-siblings", log_dir=log_dir, cwd="/cwd",
        frame_key=c.id, is_nested=True, instance_id="aaa111",
    )
    e_reporter = e_report.reporter(kind="nested_call",
                                   tasks=["/task-e"], started_at="s1")

    f = _done_node("f0000000", "/task-f", "F done", session_id="sess-F")
    f_parent = SessionRef(session_id="sess-C", file=tmp_path / "c.jsonl")
    f_tree = Tree(root_session=f_parent, nodes=[f], base_depth=1)
    f_report = InvocationReport(
        invoke_id="inv-siblings", log_dir=log_dir, cwd="/cwd",
        frame_key=c.id, is_nested=True, instance_id="bbb222",
    )
    f_reporter = f_report.reporter(kind="nested_call",
                                   tasks=["/task-f"], started_at="s2")

    root_reporter(root_tree)
    e_reporter.finalize(e_tree)
    f_reporter.finalize(f_tree)
    root_reporter.finalize(root_tree)

    # Both frame files coexist on disk — neither overwrote the other.
    frame_files = sorted(p.name for p in e_report.frames_dir.glob("*.yaml"))
    assert f"{c.id}-aaa111.yaml" in frame_files
    assert f"{c.id}-bbb222.yaml" in frame_files

    # Merged report contains BOTH children under C.
    doc = yaml.safe_load(root_report.report_path.read_text())
    c_task = next(t for t in doc["tasks"] if t["session_id"] == "sess-C")
    child_sids = {child["session_id"] for child in c_task.get("children", [])}
    assert child_sids == {"sess-E", "sess-F"}


def test_nested_reporter_is_noop_if_root_frame_absent(tmp_path):
    """If nested writes first and root hasn't landed yet, report.yaml
    should simply not be produced — we never want a partial tree without
    the root context."""
    log_dir = tmp_path / "log"
    parent = SessionRef(session_id="sess-C", file=tmp_path / "c.jsonl")
    e = _done_node("e0000000", "/task-e", "E done", session_id="sess-E")
    tree = Tree(root_session=parent, nodes=[e], base_depth=1)

    report = InvocationReport(
        invoke_id="inv-solo-nested", log_dir=log_dir, cwd="/cwd",
        frame_key="sess-C", is_nested=True,
    )
    reporter = report.reporter(kind="nested_call", tasks=["/task-e"],
                               started_at="s")
    reporter.finalize(tree)

    # Frame written, but no merged report yet.
    assert (report.frames_dir / "sess-C.yaml").exists()
    assert not report.report_path.exists()


# ---------- PERF-A: debounce + content-hash skip + frames cache ----------

def test_debounce_coalesces_burst_of_notifies(tmp_path, monkeypatch):
    """50 notifies inside a single debounce window must produce ONE atomic
    write of report.yaml, not 50. Uses a generous 2s window so the burst
    of frame YAML writes can complete on slow CI before the timer fires."""
    monkeypatch.setenv("CALLSTACK_REPORT_DEBOUNCE_SECS", "2.0")

    from agent_callstack import reporter as rep_mod

    write_count = {"n": 0}
    real_atomic_write_bytes = rep_mod._atomic_write_bytes

    def counting(path, payload):
        if path.name == "report.yaml":
            write_count["n"] += 1
        return real_atomic_write_bytes(path, payload)

    # _LiveReporter._do_merge calls the reporter-module-local symbol; patch
    # there, not on the agent_callstack re-export.
    monkeypatch.setattr(rep_mod, "_atomic_write_bytes", counting)

    log_dir = tmp_path / "log"
    parent = SessionRef(session_id="root", file=tmp_path / "r.jsonl")

    ctx = _InvocationContext(
        invoke_id="inv-burst", log_dir=log_dir, cwd="/cwd",
        frame_key=ROOT_FRAME_KEY, is_nested=False,
    )
    reporter = _LiveReporter(ctx=ctx, kind="call", tasks=["t"],
                             started_at="s")

    # Mutate the tree slightly each tick so the hash differs and we'd
    # otherwise write 50 times.
    nodes = [_done_node(f"n{i:06d}", f"task {i}", f"r{i}") for i in range(50)]
    for i in range(50):
        tree = Tree(root_session=parent, nodes=nodes[: i + 1], base_depth=0)
        reporter(tree)

    # Before debounce fires: no report write yet (frame writes don't count).
    assert write_count["n"] == 0

    # Finalize cancels the pending timer and forces a single synchronous
    # merge — so total writes after the burst is exactly 1.
    reporter.finalize(Tree(root_session=parent, nodes=nodes, base_depth=0))
    assert write_count["n"] == 1


def test_finalize_writes_report_even_after_quiet_window(tmp_path, monkeypatch):
    """finalize() must always produce an up-to-date report.yaml, even when
    no notify has happened in the last debounce window (timer already
    fired and the in-memory hash matches)."""
    monkeypatch.setenv("CALLSTACK_REPORT_DEBOUNCE_SECS", "0.05")

    log_dir = tmp_path / "log"
    parent = SessionRef(session_id="root", file=tmp_path / "r.jsonl")
    report = InvocationReport(
        invoke_id="inv-final", log_dir=log_dir, cwd="/cwd",
    )
    reporter = report.reporter(kind="call", tasks=["t"], started_at="s")
    node = _done_node("a0000000", "t", "r")
    tree = Tree(root_session=parent, nodes=[node], base_depth=0)
    reporter(tree)

    import time as _time
    _time.sleep(0.20)  # let the debounce timer fire
    first_mtime_ns = report.report_path.stat().st_mtime_ns

    # Sleep past the cache window — finalize must still rewrite.
    _time.sleep(0.05)
    reporter.finalize(tree)

    # report.yaml exists, was rewritten (mtime advanced), still complete.
    assert report.report_path.exists()
    doc = yaml.safe_load(report.report_path.read_text())
    assert doc["tasks"][0]["status"] == "complete"
    second_mtime_ns = report.report_path.stat().st_mtime_ns
    assert second_mtime_ns >= first_mtime_ns  # never went backwards


def test_content_hash_skip_avoids_duplicate_writes(tmp_path, monkeypatch):
    """Two consecutive synchronous merges of the same tree (debounce=0)
    must produce exactly one report.yaml write; the second is hash-skipped."""
    monkeypatch.setenv("CALLSTACK_REPORT_DEBOUNCE_SECS", "0")

    from agent_callstack import reporter as rep_mod

    write_count = {"n": 0}
    real_atomic_write_bytes = rep_mod._atomic_write_bytes

    def counting(path, payload):
        if path.name == "report.yaml":
            write_count["n"] += 1
        return real_atomic_write_bytes(path, payload)

    monkeypatch.setattr(rep_mod, "_atomic_write_bytes", counting)

    log_dir = tmp_path / "log"
    parent = SessionRef(session_id="root", file=tmp_path / "r.jsonl")
    ctx = _InvocationContext(
        invoke_id="inv-hash", log_dir=log_dir, cwd="/cwd",
        frame_key=ROOT_FRAME_KEY, is_nested=False,
    )
    reporter = _LiveReporter(ctx=ctx, kind="call", tasks=["t"],
                             started_at="s")
    node = _done_node("a0000000", "t", "r")
    tree = Tree(root_session=parent, nodes=[node], base_depth=0)

    # ended_at flows into the merged document, so pin it to a constant
    # across all notifies — otherwise the doc differs each tick and the
    # hash skip never triggers. _LiveReporter binds `_utc_now_iso` from
    # the reporter module; patch there.
    monkeypatch.setattr(rep_mod, "_utc_now_iso",
                        lambda: "2026-05-13T00:00:00+00:00")

    reporter(tree)
    assert write_count["n"] == 1
    # Same tree, same time → identical doc → hash match → skip.
    reporter(tree)
    reporter(tree)
    assert write_count["n"] == 1


def test_content_hash_skip_ignores_ended_at_drift(tmp_path, monkeypatch):
    """PERF-101: the skip-hash must ignore ``ended_at`` so quiet ticks
    don't rewrite report.yaml just because wall-clock advanced.

    Companion to ``test_content_hash_skip_avoids_duplicate_writes`` —
    that one pinned ``_utc_now_iso`` to a constant. This one lets time
    advance between ticks (the real production behavior) and verifies
    the second tick is still skipped because content is otherwise
    unchanged."""
    monkeypatch.setenv("CALLSTACK_REPORT_DEBOUNCE_SECS", "0")

    from agent_callstack import reporter as rep_mod

    write_count = {"n": 0}
    real_atomic_write_bytes = rep_mod._atomic_write_bytes

    def counting(path, payload):
        if path.name == "report.yaml":
            write_count["n"] += 1
        return real_atomic_write_bytes(path, payload)

    monkeypatch.setattr(rep_mod, "_atomic_write_bytes", counting)

    log_dir = tmp_path / "log"
    parent = SessionRef(session_id="root", file=tmp_path / "r.jsonl")
    ctx = _InvocationContext(
        invoke_id="inv-drift", log_dir=log_dir, cwd="/cwd",
        frame_key=ROOT_FRAME_KEY, is_nested=False,
    )
    reporter = _LiveReporter(ctx=ctx, kind="call", tasks=["t"],
                             started_at="s")
    node = _done_node("b0000000", "t", "r")
    tree = Tree(root_session=parent, nodes=[node], base_depth=0)

    # Advance the mocked clock on every tick to simulate production.
    times = iter([
        "2026-05-13T00:00:00+00:00",
        "2026-05-13T00:00:00.250000+00:00",
        "2026-05-13T00:00:00.500000+00:00",
    ])
    monkeypatch.setattr(rep_mod, "_utc_now_iso", lambda: next(times))

    reporter(tree)
    assert write_count["n"] == 1
    # Tree contents unchanged; only ended_at moves forward. Without the
    # ended_at-stripping hash, this would rewrite — regression-flag for
    # the dead skip-hash bug.
    reporter(tree)
    reporter(tree)
    assert write_count["n"] == 1, (
        f"expected hash-skip to suppress quiet-tick rewrites but saw "
        f"{write_count['n']} writes; the ended_at-stripping hash regressed"
    )


def test_frames_cache_skips_yaml_safe_load_when_stat_unchanged(
    tmp_path, monkeypatch,
):
    """_load_frames must not re-parse frame YAMLs whose (mtime_ns, size)
    matches the cached entry."""
    import agent_callstack as ac

    ac._frames_cache_clear()

    safe_load_count = {"n": 0}
    real_safe_load = yaml.safe_load

    def counting(data):
        safe_load_count["n"] += 1
        return real_safe_load(data)

    monkeypatch.setattr(yaml, "safe_load", counting)

    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    frame = {
        "frame_key": ROOT_FRAME_KEY, "is_nested": False, "kind": "call",
        "tasks": ["t"], "cwd": "/cwd",
        "started_at": "s", "ended_at": "e",
        "tree": {"schema_version": "2", "root_session_id": "r",
                  "root_session_file": "/r.jsonl", "base_depth": 0,
                  "nodes": []},
    }
    ac._atomic_yaml_write(frames_dir / "root.yaml", frame)

    safe_load_count["n"] = 0  # ignore any parsing inside _atomic_yaml_write
    out1 = ac._load_frames(frames_dir)
    after_first = safe_load_count["n"]
    assert ROOT_FRAME_KEY in out1
    assert after_first >= 1

    # Second call without touching the file: cache hit, no new parse.
    out2 = ac._load_frames(frames_dir)
    assert safe_load_count["n"] == after_first
    assert out2.keys() == out1.keys()

    # Rewrite the same file with a new mtime; cache must invalidate and
    # safe_load is called again. In production atomic-replace bumps BOTH
    # the file mtime and the directory mtime — the dir-mtime fast-path
    # (PERF-104) would otherwise short-circuit before we reach the
    # per-file cache check. Simulate that by bumping both.
    import os as _os
    import time as _time
    new_mtime = _time.time() + 10  # bump mtime forward
    _os.utime(frames_dir / "root.yaml", (new_mtime, new_mtime))
    _os.utime(frames_dir, (new_mtime, new_mtime))
    ac._load_frames(frames_dir)
    assert safe_load_count["n"] > after_first


def test_load_frames_dir_mtime_fast_path_skips_glob(tmp_path, monkeypatch):
    """PERF-104: when the frames-dir mtime is unchanged, `_load_frames`
    should reuse the cached aggregate and skip the per-file glob + stat
    entirely."""
    import agent_callstack as ac
    from agent_callstack import frames as frames_mod

    ac._frames_cache_clear()

    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    frame = {
        "frame_key": ROOT_FRAME_KEY, "is_nested": False, "kind": "call",
        "tasks": ["t"], "cwd": "/cwd",
        "started_at": "s", "ended_at": "e",
        "tree": {"schema_version": "2", "root_session_id": "r",
                 "root_session_file": "/r.jsonl", "base_depth": 0,
                 "nodes": []},
    }
    ac._atomic_yaml_write(frames_dir / "root.yaml", frame)

    # Prime the dir cache.
    out1 = ac._load_frames(frames_dir)
    assert ROOT_FRAME_KEY in out1

    # Spy on the glob — second call must NOT iterate frames_dir.
    glob_calls = {"n": 0}
    real_glob = frames_mod.Path.glob

    def counting_glob(self, pattern):
        if self == frames_dir:
            glob_calls["n"] += 1
        return real_glob(self, pattern)

    monkeypatch.setattr(frames_mod.Path, "glob", counting_glob)

    out2 = ac._load_frames(frames_dir)
    assert glob_calls["n"] == 0, (
        f"expected dir-mtime fast-path to skip glob, but glob was called "
        f"{glob_calls['n']} times"
    )
    assert out2.keys() == out1.keys()


def test_load_frames_dir_cache_returns_independent_copy(tmp_path):
    """The dir-mtime cache must hand back a copy callers can mutate
    without poisoning the cached snapshot for the next call."""
    import agent_callstack as ac
    ac._frames_cache_clear()
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    ac._atomic_yaml_write(frames_dir / "root.yaml", {
        "frame_key": ROOT_FRAME_KEY, "is_nested": False, "kind": "call",
        "tasks": ["t"], "cwd": "/cwd", "started_at": "s", "ended_at": "e",
        "tree": {"schema_version": "2", "root_session_id": "r",
                 "root_session_file": "/r.jsonl", "base_depth": 0,
                 "nodes": []},
    })

    out1 = ac._load_frames(frames_dir)
    out1[ROOT_FRAME_KEY].clear()  # corrupt the returned dict
    out2 = ac._load_frames(frames_dir)
    assert out2[ROOT_FRAME_KEY], (
        "the dir cache returned the same list object twice — a caller "
        "that mutates the result corrupts the cache for the next call"
    )


def test_parsed_frame_cache_lru_eviction(tmp_path):
    """PERF-103: `_FRAMES_PARSED_CACHE` is bounded — past the cap,
    least-recently-used entries are evicted instead of growing
    monotonically across long-lived MCP server lifetimes."""
    from agent_callstack import frames as frames_mod
    import agent_callstack as ac
    ac._frames_cache_clear()

    # Temporarily shrink the cap so the test stays small.
    original = frames_mod._FRAMES_PARSED_CACHE_MAX
    frames_mod._FRAMES_PARSED_CACHE_MAX = 3
    try:
        with frames_mod._FRAMES_PARSED_CACHE_LOCK:
            for i in range(5):
                p = tmp_path / f"f{i}.yaml"
                frames_mod._cache_put_parsed(p, (i, i, {"frame_key": str(i)}))
        with frames_mod._FRAMES_PARSED_CACHE_LOCK:
            assert len(frames_mod._FRAMES_PARSED_CACHE) == 3
            keys = list(frames_mod._FRAMES_PARSED_CACHE.keys())
            # The three most recently inserted (2, 3, 4) survive; 0 and 1 evicted.
            assert keys[-1] == tmp_path / "f4.yaml"
            assert tmp_path / "f0.yaml" not in frames_mod._FRAMES_PARSED_CACHE
    finally:
        frames_mod._FRAMES_PARSED_CACHE_MAX = original
        ac._frames_cache_clear()
