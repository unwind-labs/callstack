"""Tests for the per-invocation YAML report writer."""
from __future__ import annotations

import json

import yaml

from agent_callstack import (
    _InvocationContext, _LiveReporter, _ROOT_FRAME_KEY,
    _write_invocation_report,
)
from agent_callstack.channel import ScriptedChannel
from agent_callstack.driver import Driver, Node, Tree
from agent_callstack.session import SessionLocator, SessionRef
from agent_callstack.trace import TraceWriter, TreeStore
from agent_callstack import state as st


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

def test_driver_progress_callback_fires_per_transition(tmp_path):
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

    ctx = _InvocationContext(
        invoke_id="inv-live", log_dir=log_dir, cwd=str(tmp_path),
        frame_key=_ROOT_FRAME_KEY, is_nested=False,
    )
    driver = Driver(
        channel=channel,
        locator=SessionLocator(projects_dir=tmp_path / "projects"),
        trace=TraceWriter(ctx.invocation_dir),
        store=TreeStore(),
        cwd=str(tmp_path), timeout=10, max_depth=5,
    )
    reporter = _LiveReporter(ctx=ctx, kind="call", tasks=["do it"], started_at="s")

    def spy(tree):
        reporter(tree)
        snapshots.append(ctx.report_path.read_text())

    driver.on_progress = spy
    tree = driver.run(parent, ["do it"])
    reporter.finalize(tree)

    assert len(snapshots) >= 2
    first = yaml.safe_load(snapshots[0])
    last = yaml.safe_load(ctx.report_path.read_text())
    assert first["tasks"][0]["status"] == "pending"
    assert last["tasks"][0]["status"] == "complete"
    assert last["tasks"][0]["output"] == "done"

    # Append-only log (tail-friendly) sits inside the invocation dir.
    log_lines = [ln for ln in ctx.log_path.read_text().splitlines() if ln]
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

    root_ctx = _InvocationContext(
        invoke_id="inv-merge", log_dir=log_dir, cwd="/cwd",
        frame_key=_ROOT_FRAME_KEY, is_nested=False,
    )
    root_reporter = _LiveReporter(ctx=root_ctx, kind="call",
                                  tasks=["/task-b", "/task-c", "/task-d"],
                                  started_at="s")

    # Nested invocation (from inside C's subprocess): E, F
    e = _done_node("e0000000", "/task-e", "E done", session_id="sess-E")
    f = _done_node("f0000000", "/task-f", "F done", session_id="sess-F")
    nested_parent = SessionRef(session_id="sess-C", file=tmp_path / "c.jsonl")
    nested_tree = Tree(root_session=nested_parent, nodes=[e, f], base_depth=1)

    nested_ctx = _InvocationContext(
        invoke_id="inv-merge", log_dir=log_dir, cwd="/cwd",
        frame_key="sess-C",  # the calling session
        is_nested=True,
    )
    nested_reporter = _LiveReporter(ctx=nested_ctx, kind="nested_call",
                                    tasks=["/task-e", "/task-f"], started_at="s")

    # Realistic ordering: root writes at least one snapshot (so root.yaml
    # exists with C), nested fires during C's turn, root finalizes.
    root_reporter(root_tree)
    nested_reporter.finalize(nested_tree)
    root_reporter.finalize(root_tree)

    doc = yaml.safe_load(root_ctx.report_path.read_text())
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
    log_text = root_ctx.log_path.read_text()
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
    root_ctx = _InvocationContext(
        invoke_id="inv-3lvl", log_dir=log_dir, cwd="/cwd",
        frame_key=_ROOT_FRAME_KEY, is_nested=False,
    )
    root_reporter = _LiveReporter(ctx=root_ctx, kind="call",
                                  tasks=["/task-c"], started_at="s")

    # Level-2 frame: E (caller = C)
    e = _done_node("E0000000", "/task-e", "E done", session_id="sess-E")
    e_parent = SessionRef(session_id="sess-C", file=tmp_path / "c.jsonl")
    e_tree = Tree(root_session=e_parent, nodes=[e], base_depth=1)
    e_ctx = _InvocationContext(
        invoke_id="inv-3lvl", log_dir=log_dir, cwd="/cwd",
        frame_key=c.id, is_nested=True,
    )
    e_reporter = _LiveReporter(ctx=e_ctx, kind="nested_call",
                               tasks=["/task-e"], started_at="s")

    # Level-3 frame: G (caller = E)
    g = _done_node("G0000000", "/task-g", "G done", session_id="sess-G")
    g_parent = SessionRef(session_id="sess-E", file=tmp_path / "e.jsonl")
    g_tree = Tree(root_session=g_parent, nodes=[g], base_depth=2)
    g_ctx = _InvocationContext(
        invoke_id="inv-3lvl", log_dir=log_dir, cwd="/cwd",
        frame_key=e.id, is_nested=True,
    )
    g_reporter = _LiveReporter(ctx=g_ctx, kind="nested_call",
                               tasks=["/task-g"], started_at="s")

    # Order matters for chain lookups: root and E must exist on disk
    # before G looks up E's ancestry.
    root_reporter(root_tree)
    e_reporter(e_tree)
    g_reporter.finalize(g_tree)
    e_reporter.finalize(e_tree)
    root_reporter.finalize(root_tree)

    log_text = root_ctx.log_path.read_text()
    assert "[C0000000→E0000000→G0000000]" in log_text, (
        f"missing 3-level chain in log:\n{log_text}"
    )
    # Also check the grafted report: G nested under E nested under C.
    doc = yaml.safe_load(root_ctx.report_path.read_text())
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

    root_ctx = _InvocationContext(
        invoke_id="inv-by-nodeid", log_dir=log_dir, cwd="/cwd",
        frame_key=_ROOT_FRAME_KEY, is_nested=False,
    )
    root_reporter = _LiveReporter(ctx=root_ctx, kind="call",
                                  tasks=["/task-b", "/task-c", "/task-d"],
                                  started_at="s")

    e = _done_node("eNODE000", "/task-e", "E done", session_id="sess-E")
    f = _done_node("fNODE000", "/task-f", "F done", session_id="sess-F")
    nested_parent = SessionRef(session_id="sess-C", file=tmp_path / "c.jsonl")
    nested_tree = Tree(root_session=nested_parent, nodes=[e, f], base_depth=1)

    # Nested frame keyed by C's full node id (what CALLSTACK_FRAME_KEY holds).
    nested_ctx = _InvocationContext(
        invoke_id="inv-by-nodeid", log_dir=log_dir, cwd="/cwd",
        frame_key=c.id,
        is_nested=True,
    )
    nested_reporter = _LiveReporter(ctx=nested_ctx, kind="nested_call",
                                    tasks=["/task-e", "/task-f"], started_at="s")

    root_reporter(root_tree)
    nested_reporter.finalize(nested_tree)
    root_reporter.finalize(root_tree)

    doc = yaml.safe_load(root_ctx.report_path.read_text())
    c_task = next(t for t in doc["tasks"] if t["session_id"] == "sess-C")
    assert {child["session_id"] for child in c_task["children"]} == {"sess-E", "sess-F"}
    # Log chain prefixed with C's short id.
    log_text = root_ctx.log_path.read_text()
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

    monkeypatch.setattr("agent_callstack.PROJECTS_DIR", projects)
    monkeypatch.setenv(ENV_ROOT_INVOKE_ID, "inv-nest-fallback")
    monkeypatch.setenv(ENV_ROOT_LOG_DIR, str(tmp_path / "log"))
    monkeypatch.delenv(ENV_CLAUDE_SESSION, raising=False)

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

    root_ctx = _InvocationContext(
        invoke_id="inv-siblings", log_dir=log_dir, cwd="/cwd",
        frame_key=_ROOT_FRAME_KEY, is_nested=False,
    )
    root_reporter = _LiveReporter(ctx=root_ctx, kind="call",
                                  tasks=["/task-c"], started_at="s0")

    # Two sibling nested invocations from inside C, each with its own
    # children. Same frame_key (C's id), distinct instance_ids → distinct
    # frame files.
    e = _done_node("e0000000", "/task-e", "E done", session_id="sess-E")
    e_parent = SessionRef(session_id="sess-C", file=tmp_path / "c.jsonl")
    e_tree = Tree(root_session=e_parent, nodes=[e], base_depth=1)
    e_ctx = _InvocationContext(
        invoke_id="inv-siblings", log_dir=log_dir, cwd="/cwd",
        frame_key=c.id, is_nested=True, instance_id="aaa111",
    )
    e_reporter = _LiveReporter(ctx=e_ctx, kind="nested_call",
                               tasks=["/task-e"], started_at="s1")

    f = _done_node("f0000000", "/task-f", "F done", session_id="sess-F")
    f_parent = SessionRef(session_id="sess-C", file=tmp_path / "c.jsonl")
    f_tree = Tree(root_session=f_parent, nodes=[f], base_depth=1)
    f_ctx = _InvocationContext(
        invoke_id="inv-siblings", log_dir=log_dir, cwd="/cwd",
        frame_key=c.id, is_nested=True, instance_id="bbb222",
    )
    f_reporter = _LiveReporter(ctx=f_ctx, kind="nested_call",
                               tasks=["/task-f"], started_at="s2")

    root_reporter(root_tree)
    e_reporter.finalize(e_tree)
    f_reporter.finalize(f_tree)
    root_reporter.finalize(root_tree)

    # Both frame files coexist on disk — neither overwrote the other.
    frame_files = sorted(p.name for p in e_ctx.frames_dir.glob("*.yaml"))
    assert f"{c.id}-aaa111.yaml" in frame_files
    assert f"{c.id}-bbb222.yaml" in frame_files

    # Merged report contains BOTH children under C.
    doc = yaml.safe_load(root_ctx.report_path.read_text())
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

    ctx = _InvocationContext(
        invoke_id="inv-solo-nested", log_dir=log_dir, cwd="/cwd",
        frame_key="sess-C", is_nested=True,
    )
    reporter = _LiveReporter(ctx=ctx, kind="nested_call", tasks=["/task-e"],
                             started_at="s")
    reporter.finalize(tree)

    # Frame written, but no merged report yet.
    assert (ctx.frames_dir / "sess-C.yaml").exists()
    assert not ctx.report_path.exists()
