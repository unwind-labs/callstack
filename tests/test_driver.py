"""Driver tests with ScriptedChannel — exercises the full state machine end-to-end
without spawning any subprocess."""

from __future__ import annotations

import json
from pathlib import Path

import agent_callstack.state as st
import pytest
from agent_callstack.channel import ScriptedChannel, TurnResult, TurnTimeout
from agent_callstack.driver import Driver, Node, Tree, _TreeIndex
from agent_callstack.session import SessionLocator, SessionRef
from agent_callstack.trace import TraceWriter, TreeStore

# ---------- helpers ----------


def _envelope(op: str, **fields) -> str:
    return "```json\n" + json.dumps({"op": op, **fields}) + "\n```"


@pytest.fixture
def parent_session(tmp_path) -> SessionRef:
    f = tmp_path / "parent.jsonl"
    f.write_text("line1\nline2\nline3\n")
    return SessionRef(session_id="00000000-0000-0000-0000-0000000000d2", file=f)


def _make_driver(tmp_path, channel: ScriptedChannel, *, max_depth: int = 5) -> Driver:
    return Driver(
        channel=channel,
        resolve_session=SessionLocator(projects_dir=tmp_path / "_no_real_projects").resolve,
        trace=TraceWriter(tmp_path / "traces"),
        store=TreeStore(),
        cwd=str(tmp_path),
        timeout=10,
        max_depth=max_depth,
    )


# ---------- single-task happy path ----------


class TestRunSingleTask:
    def test_complete_directly(self, tmp_path, parent_session):
        ch = ScriptedChannel().respond(_envelope("return", result="hello"), "child-1")
        driver = _make_driver(tmp_path, ch)

        tree = driver.run(parent_session, ["do thing"])

        assert len(tree.nodes) == 1
        node = tree.nodes[0]
        assert node.status == "complete"
        assert node.result == "hello"
        assert node.session_id == "child-1"
        assert node.parent_lines == 3  # parent file had 3 lines
        # The first call should be a fork.
        assert ch.log[0][2] == "fork"
        assert node.call_type == "fork"

    def test_fresh_context_emits_fresh_mode(self, tmp_path, parent_session):
        ch = ScriptedChannel().respond(_envelope("return", result="hi"), "child-x")
        driver = _make_driver(tmp_path, ch)

        tree = driver.run(parent_session, ["task"], context="fresh")
        node = tree.nodes[0]

        assert ch.log[0][2] == "fresh"
        # Same project (driver.cwd is tmp_path; parent.cwd is also tmp_path
        # because the parent JSONL is written there) → call_type is "fresh".
        assert node.call_type == "fresh"
        # Fresh sessions have no inherited transcript.
        assert node.parent_lines == 0

    def test_fresh_cross_project_call_type(self, tmp_path):
        # Parent session lives in tmp_path/A; driver.cwd points at tmp_path/B.
        proj_a = tmp_path / "A"
        proj_b = tmp_path / "B"
        proj_a.mkdir()
        proj_b.mkdir()
        parent_file = proj_a / "parent.jsonl"
        parent_file.write_text(json.dumps({"cwd": str(proj_a), "type": "user"}) + "\n")
        parent = SessionRef(session_id="p", file=parent_file)

        ch = ScriptedChannel().respond(_envelope("return", result="ok"), "child-y")
        driver = Driver(
            channel=ch,
            resolve_session=SessionLocator(projects_dir=tmp_path / "_proj").resolve,
            trace=TraceWriter(tmp_path / "traces"),
            store=TreeStore(),
            cwd=str(proj_b),
            timeout=10,
        )

        tree = driver.run(parent, ["task"], context="fresh")
        node = tree.nodes[0]
        assert ch.log[0][2] == "fresh"
        assert node.call_type == "fresh_cross_project"

    def test_invalid_context_raises(self, tmp_path, parent_session):
        ch = ScriptedChannel()
        driver = _make_driver(tmp_path, ch)
        with pytest.raises(ValueError):
            driver.run(parent_session, ["t"], context="bogus")

    def test_yield_pauses_tree(self, tmp_path, parent_session):
        ch = ScriptedChannel().respond(_envelope("yield", question="MFA?"), "child-1")
        driver = _make_driver(tmp_path, ch)

        tree = driver.run(parent_session, ["auth"])

        node = tree.nodes[0]
        assert node.status == "yielded"
        assert node.session_id == "child-1"
        assert tree.yielded_leaves() == [node]

    def test_call_then_complete(self, tmp_path, parent_session):
        """Parent CALLs, child returns, parent resumes and returns."""
        ch = (
            ScriptedChannel()
            .respond(_envelope("call", task="sub-task"), "parent-fork")
            .respond(_envelope("return", result="sub-result"), "00000000-0000-0000-0000-0000000000d1")
            .respond(_envelope("return", result="all done"), "parent-fork")
        )
        driver = _make_driver(tmp_path, ch)

        tree = driver.run(parent_session, ["main"])

        root = tree.nodes[0]
        assert root.status == "complete"
        assert root.result == "all done"
        assert len(root.children) == 1
        assert root.children[0].result == "sub-result"
        # Parent's resume must be a resume; child's first turn is a fork.
        modes = [entry[2] for entry in ch.log]
        assert modes == ["fork", "fork", "resume"]

    def test_child_yield_propagates_pause(self, tmp_path, parent_session):
        """Child yields → tree is paused; parent stays in awaiting_child."""
        ch = (
            ScriptedChannel()
            .respond(_envelope("call", task="sub"), "parent-fork")
            .respond(_envelope("yield", question="Code?"), "00000000-0000-0000-0000-0000000000d1")
        )
        driver = _make_driver(tmp_path, ch)

        tree = driver.run(parent_session, ["main"])

        root = tree.nodes[0]
        assert root.status == "running"  # awaiting_child
        child = root.children[0]
        assert child.status == "yielded"
        assert tree.yielded_leaves() == [child]

    def test_invocation_error(self, tmp_path, parent_session):
        def boom(*_):
            raise RuntimeError("CLI not found")

        ch = ScriptedChannel().respond_with(boom)
        driver = _make_driver(tmp_path, ch)

        tree = driver.run(parent_session, ["doomed"])

        node = tree.nodes[0]
        assert node.status == "error"
        assert "Invocation failed" in node.error

    def test_timeout(self, tmp_path, parent_session):
        def slow(*_):
            raise TurnTimeout("turn timed out after 10s", partial="some text")

        ch = ScriptedChannel().respond_with(slow)
        driver = _make_driver(tmp_path, ch)

        tree = driver.run(parent_session, ["slow"])

        node = tree.nodes[0]
        assert node.status == "error"
        assert "timed out" in node.error

    def test_upstream_rate_limit_classified(self, tmp_path, parent_session):
        """When the child returns Claude Code's synthetic rate-limit text,
        the driver must surface it as `upstream_rate_limited:` rather than
        the generic envelope-parse failure — so the parent agent can act
        on a transient upstream condition."""
        synthetic = "API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited"
        ch = ScriptedChannel().respond(synthetic, "child-rl")
        driver = _make_driver(tmp_path, ch)

        tree = driver.run(parent_session, ["task"])

        node = tree.nodes[0]
        assert node.status == "error"
        assert node.error.startswith("upstream_rate_limited:")
        assert "Server is temporarily limiting requests" in node.error

    def test_unparseable_non_rate_limit_falls_through(self, tmp_path, parent_session):
        """Plain garbage that isn't a recognized synthetic must still surface
        the existing 'no parseable envelope' error — regression guard that
        the new classifier branch didn't widen."""
        ch = ScriptedChannel().respond("hello, no envelope here", "child-junk")
        driver = _make_driver(tmp_path, ch)

        tree = driver.run(parent_session, ["task"])

        node = tree.nodes[0]
        assert node.status == "error"
        assert node.error == "child emitted no parseable envelope"


# ---------- parallel root tasks ----------


class TestParallel:
    def test_two_tasks_complete(self, tmp_path, parent_session):
        # Order across threads is non-deterministic; key on the unique tail of the
        # prompt (which always ends with the task text after a "\n\n").
        marker_to_response = {
            "Task: task ALPHA": _envelope("return", result="ALPHA done"),
            "Task: task BRAVO": _envelope("return", result="BRAVO done"),
        }

        def respond(_src, prompt, _fork):
            tail = prompt.rsplit("\n\n", 1)[-1]
            body = marker_to_response.get(tail)
            if body is None:
                raise AssertionError(f"unrecognized task tail: {tail!r}")
            sid = "alpha-fork" if "ALPHA" in tail else "bravo-fork"
            return TurnResult(
                text=body,
                session_id=sid,
                duration=0.0,
                api_request_id="",
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                total_cost_usd=0.0,
            )

        ch = ScriptedChannel().respond_with(respond).respond_with(respond)
        driver = _make_driver(tmp_path, ch)

        tree = driver.run(parent_session, ["task ALPHA", "task BRAVO"])

        assert len(tree.nodes) == 2
        assert {n.result for n in tree.nodes} == {"ALPHA done", "BRAVO done"}
        assert all(n.status == "complete" for n in tree.nodes)

    def test_propagate_up_serializes_concurrent_callers(self, tmp_path, parent_session, monkeypatch):
        """CONC-3: ``_propagate_up`` is guarded by an RLock so concurrent
        producers of ChildDone/ChildFailed can't double-step the same
        ancestor. Verify the lock actually serializes — only one thread
        inside the critical section at a time."""
        import threading

        ch = ScriptedChannel().respond(_envelope("return", result="ok"), "x-fork")
        driver = _make_driver(tmp_path, ch)
        tree = Tree(root_session=parent_session, nodes=[], base_depth=0)
        # Spy: count concurrent occupants of `_propagate_up`. The lock
        # is acquired at function entry; this hook fires AFTER the
        # acquire, so seeing concurrent>1 here means the lock failed.
        concurrent = 0
        peak = 0
        cond = threading.Lock()
        gate = threading.Event()

        real_build = _TreeIndex.build

        def slow_build(t):
            nonlocal concurrent, peak
            with cond:
                concurrent += 1
                peak = max(peak, concurrent)
            gate.wait(timeout=0.5)
            try:
                return real_build(t)
            finally:
                with cond:
                    concurrent -= 1

        import agent_callstack.driver as drv_mod

        # monkeypatch restores _TreeIndex.build automatically at teardown,
        # so a mid-test failure can't leak the slow stub into other tests.
        monkeypatch.setattr(drv_mod._TreeIndex, "build", staticmethod(slow_build))
        t1 = threading.Thread(
            target=lambda: driver._propagate_up(tree, None),  # type: ignore[arg-type]
        )
        t2 = threading.Thread(
            target=lambda: driver._propagate_up(tree, None),  # type: ignore[arg-type]
        )
        t1.start()
        t2.start()
        gate.set()
        t1.join()
        t2.join()

        assert peak == 1, (
            f"_propagate_up allowed {peak} concurrent threads inside; the CONC-3 lock failed to serialize."
        )

    def test_one_sibling_raises_others_still_return(self, tmp_path, parent_session):
        """CORR-104: when one sibling's `_drive` raises (e.g. unexpected
        resolver / OS error), the others must still complete and their
        results survive in the tree. The failing sibling lands in
        ``Failed`` state with the exception text — not lost on the
        worker thread."""

        def respond(_src, prompt, _fork):
            tail = prompt.rsplit("\n\n", 1)[-1]
            if "EXPLODE" in tail:
                raise RuntimeError("simulated worker crash")
            return TurnResult(
                text=_envelope("return", result="ok"),
                session_id="ok-fork",
                duration=0.0,
                api_request_id="",
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                total_cost_usd=0.0,
            )

        ch = ScriptedChannel().respond_with(respond).respond_with(respond).respond_with(respond)
        driver = _make_driver(tmp_path, ch)

        tree = driver.run(parent_session, ["task ALPHA", "task EXPLODE", "task BRAVO"])

        # Three nodes, exactly one failed. The other two return real
        # results — regression-flag for the bug where the first raised
        # exception aborted the result-collection loop.
        statuses = [n.status for n in tree.nodes]
        assert statuses.count("error") == 1, f"expected exactly one error node, got {statuses}"
        assert statuses.count("complete") == 2

        failed = next(n for n in tree.nodes if n.status == "error")
        assert "simulated worker crash" in (failed.error or "")


# ---------- parent-session invariant ----------


class TestParentSessionInvariant:
    """The core /call invariant at the Driver layer: `Driver.run(parent=P)`
    forks from P's session, regardless of what env vars are present. The
    Driver must NOT consult process env for parent identity."""

    def test_forks_from_supplied_parent_ignoring_stale_env(self, tmp_path, parent_session, monkeypatch):
        # Plant stale values that resemble the recursive-/call scenario.
        monkeypatch.setenv("CALLSTACK_PARENT_SESSION", "/some/stale/path.jsonl")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "00000000-0000-0000-0000-0000000000ee")

        ch = ScriptedChannel().respond(_envelope("return", result="ok"), "c1")
        driver = _make_driver(tmp_path, ch)
        driver.run(parent_session, ["task"])

        # The Driver must use the SessionRef it was handed, not env.
        assert ch.log[0][0] == parent_session.session_id, (
            f"Driver forked from {ch.log[0][0]} but was given "
            f"{parent_session.session_id} — Driver must not consult env "
            f"for parent identity"
        )


# ---------- max-depth enforcement ----------


class TestMaxDepth:
    def test_root_below_limit_proceeds(self, tmp_path, parent_session):
        ch = ScriptedChannel().respond(_envelope("return", result="ok"), "f")
        driver = _make_driver(tmp_path, ch, max_depth=3)
        tree = driver.run(parent_session, ["t"], base_depth=0)
        assert tree.nodes[0].status == "complete"

    def test_root_at_limit_fails(self, tmp_path, parent_session):
        ch = ScriptedChannel()
        driver = _make_driver(tmp_path, ch, max_depth=3)
        # base_depth=3 means starting depth would be 4 > 3.
        tree = driver.run(parent_session, ["t"], base_depth=3)
        assert tree.nodes[0].status == "error"
        assert "depth" in tree.nodes[0].error.lower()
        assert ch.log == []  # no turns issued


# ---------- resume ----------


class TestResume:
    def test_yield_persists_then_resume_completes(self, tmp_path, parent_session):
        """Driver.run yields → snapshot saved → resume continues."""
        # Prepare a clone path the locator can resolve. After SEC-003,
        # resolve(cwd=...) only looks in the cwd-matching project dir,
        # so the clone must live there.
        from agent_callstack.session import encode_project_dir

        project_dir = tmp_path / "_no_real_projects" / encode_project_dir(str(tmp_path))
        project_dir.mkdir(parents=True)
        clone_path = project_dir / "00000000-0000-0000-0000-0000000000d1.jsonl"
        clone_path.write_text("")

        ch = ScriptedChannel().respond(_envelope("yield", question="Code?"), "00000000-0000-0000-0000-0000000000d1")
        driver = _make_driver(tmp_path, ch)
        tree = driver.run(parent_session, ["auth"])
        leaf = tree.yielded_leaves()[0]
        assert Path(leaf.clone_path or "") == clone_path

        # Sidecar was saved.
        store = TreeStore()
        snapshot = store.load(clone_path)
        assert snapshot is not None
        loaded = Tree.from_dict(snapshot)
        assert loaded.yielded_leaves()[0].session_id == "00000000-0000-0000-0000-0000000000d1"

        # Resume the leaf.
        ch.respond(_envelope("return", result="authenticated"), "00000000-0000-0000-0000-0000000000d1")
        driver.resume(loaded, target_session_id="00000000-0000-0000-0000-0000000000d1", reply="847291")

        leaf2 = loaded.nodes[0]
        assert leaf2.status == "complete"
        assert leaf2.result == "authenticated"
        # The resume turn must use mode="resume", not a new fork.
        last_call = ch.log[-1]
        assert last_call[2] == "resume"
        assert last_call[1] == "847291"

    def test_resume_unblocks_parent(self, tmp_path, parent_session):
        """Child yields, blocking parent. After child resume, parent is unblocked."""
        from agent_callstack.session import encode_project_dir

        project_dir = tmp_path / "_no_real_projects" / encode_project_dir(str(tmp_path))
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "00000000-0000-0000-0000-0000000000d4.jsonl").write_text("")
        (project_dir / "00000000-0000-0000-0000-0000000000d1.jsonl").write_text("")

        ch = (
            ScriptedChannel()
            .respond(_envelope("call", task="sub"), "00000000-0000-0000-0000-0000000000d4")
            .respond(_envelope("yield", question="MFA?"), "00000000-0000-0000-0000-0000000000d1")
        )
        driver = _make_driver(tmp_path, ch)
        tree = driver.run(parent_session, ["main"])
        root, child = tree.nodes[0], tree.nodes[0].children[0]
        assert child.status == "yielded"
        assert root.status == "running"

        # Resume the child: it returns, parent should resume and complete.
        ch.respond(_envelope("return", result="auth-ok"), "00000000-0000-0000-0000-0000000000d1")
        ch.respond(_envelope("return", result="all done"), "00000000-0000-0000-0000-0000000000d4")
        driver.resume(tree, target_session_id="00000000-0000-0000-0000-0000000000d1", reply="847291")

        assert child.status == "complete"
        assert root.status == "complete"
        assert root.result == "all done"


# ---------- paper-v1 instrumentation ----------


class TestInstrumentation:
    def test_max_context_tokens_seen_includes_cache_reads(self, tmp_path, parent_session):
        """Effective context = input_tokens + cache_read_tokens. A turn with
        5 uncached input tokens and 20000 cache-read tokens must report peak
        of 20005, not 5 — cache reads are still IN the model's context."""

        def respond(_src, _prompt, _fork):
            return TurnResult(
                text=_envelope("return", result="ok"),
                session_id="f",
                duration=0.0,
                api_request_id="req",
                input_tokens=5,
                output_tokens=1,
                cache_read_tokens=20000,
                cache_creation_tokens=100,
                total_cost_usd=0.0,
            )

        ch = ScriptedChannel().respond_with(respond)
        driver = _make_driver(tmp_path, ch)
        tree = driver.run(parent_session, ["t"])
        assert tree.nodes[0].max_context_tokens_seen == 20005

    def test_max_context_tokens_seen_tracks_peak(self, tmp_path, parent_session):
        """Node.max_context_tokens_seen should track the max input_tokens across turns."""

        def respond(src, prompt, fork):
            # Return increasing then decreasing input_tokens to verify we keep the peak.
            sequence = [1000, 5000, 3000]
            idx = len(ch.log) - 1  # log has already been appended before respond runs
            toks = sequence[min(idx, len(sequence) - 1)]
            if idx == 0:
                body = _envelope("call", task="sub")
                sid = "parent-fork"
            elif idx == 1:
                body = _envelope("return", result="sub-done")
                sid = "00000000-0000-0000-0000-0000000000d1"
            else:
                body = _envelope("return", result="all done")
                sid = "parent-fork"
            return TurnResult(
                text=body,
                session_id=sid,
                duration=0.0,
                api_request_id=f"req_{idx}",
                input_tokens=toks,
                output_tokens=100,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                total_cost_usd=0.0,
            )

        ch = ScriptedChannel().respond_with(respond).respond_with(respond).respond_with(respond)
        driver = _make_driver(tmp_path, ch)

        tree = driver.run(parent_session, ["main"])
        root = tree.nodes[0]
        # Root saw 1000 on first turn, then 3000 on resume (after child returned).
        # Peak across both is 3000.
        assert root.max_context_tokens_seen == 3000
        # Child saw 5000 on its single turn.
        assert root.children[0].max_context_tokens_seen == 5000

    def test_tree_schema_version_in_snapshot(self, tmp_path, parent_session):
        ch = ScriptedChannel().respond(_envelope("yield", question="?"), "00000000-0000-0000-0000-0000000000d1")
        driver = _make_driver(tmp_path, ch)
        tree = driver.run(parent_session, ["t"])
        assert tree.to_dict()["schema_version"] == "2"

    def test_tree_from_dict_rejects_wrong_schema(self):
        with pytest.raises(ValueError, match="schema_version"):
            Tree.from_dict(
                {
                    "schema_version": "1",
                    "root_session_id": "x",
                    "root_session_file": "/tmp/x",
                    "base_depth": 0,
                    "nodes": [],
                }
            )

    def test_tree_from_dict_rejects_missing_schema(self):
        with pytest.raises(ValueError, match="schema_version"):
            Tree.from_dict(
                {
                    "root_session_id": "x",
                    "root_session_file": "/tmp/x",
                    "base_depth": 0,
                    "nodes": [],
                }
            )

    def test_node_from_dict_tolerates_missing_max_context_tokens(self):
        # `max_context_tokens_seen` was added *within* schema v2, so the
        # version gate doesn't protect against early-v2 snapshots that
        # predate the field. resume() deserializes exactly these snapshots,
        # so a missing key must default (to 0) rather than raise KeyError
        # and abort the resume. Regression for /recheck H1.
        d = Node(id="a" * 32, task="t", state=st.Done(result="ok")).to_dict()
        del d["max_context_tokens_seen"]
        node = Node.from_dict(d)
        assert node.max_context_tokens_seen == 0

    def test_driver_seed_forwards_to_trace(self, tmp_path, parent_session):
        ch = ScriptedChannel().respond(_envelope("return", result="ok"), "f")
        driver = Driver(
            channel=ch,
            resolve_session=SessionLocator(projects_dir=tmp_path / "_no_real_projects").resolve,
            trace=TraceWriter(tmp_path / "traces"),
            store=TreeStore(),
            cwd=str(tmp_path),
            timeout=10,
            max_depth=5,
            seed=7,
        )
        driver.run(parent_session, ["t"])
        entry = json.loads((tmp_path / "traces" / "call_trace.jsonl").read_text().strip().split("\n")[0])
        assert entry["seed"] == 7


# ---------- ARCH-3: deep propagate_up via _TreeIndex ----------


class TestDeepPropagate:
    """Pin correctness of the index-driven ancestor walk on a deeper chain
    than other tests reach. Pre-ARCH-3, _propagate_up did three O(N) walks
    per loop iteration; this test catches index-build / lookup regressions.

    `_propagate_up` only runs during `resume()`, so we build a chain of N
    CALLs ending in a YIELD at the deepest leaf, then resume — that's the
    only path that exercises the index."""

    def test_depth_10_chain_propagates_through_resume(self, tmp_path, parent_session):
        depth = 10
        # Going down: each ancestor emits CALL on its first turn.
        # Leaf YIELDs (pauses the whole chain).
        # On resume: leaf RETURNs with the reply, then each ancestor's
        # "child returned" resume turn emits its own RETURN — bubbling
        # back up via _propagate_up.
        ch = ScriptedChannel()
        for i in range(depth - 1):
            ch.respond(_envelope("call", task=f"level-{i + 1}"), f"sess-{i}")
        # Leaf yields:
        ch.respond(_envelope("yield", question="ok?"), f"sess-{depth - 1}")
        # After resume(): leaf returns, then ancestors return in unwind order
        # (deepest ancestor first up to the root).
        ch.respond(_envelope("return", result="leaf-after-resume"), f"sess-{depth - 1}")
        for i in reversed(range(depth - 1)):
            ch.respond(_envelope("return", result=f"r{i}"), f"sess-{i}")

        driver = _make_driver(tmp_path, ch, max_depth=depth + 2)
        tree = driver.run(parent_session, ["root-task"])

        # After run(): the whole chain is built but stalled on the leaf yield.
        # Each ancestor is AwaitingChild; leaf is AwaitingUser.
        leaf_sid = f"sess-{depth - 1}"
        leaf = tree.find_by_session(leaf_sid)
        assert leaf is not None
        assert leaf.state.kind == "awaiting_user"

        # Resume — this triggers _propagate_up, exercising _TreeIndex over
        # a depth-10 ancestor chain.
        driver.resume(tree, leaf_sid, "go")

        # Walk down and confirm every node landed in `done` with the
        # right result.
        node = tree.nodes[0]
        for i in range(depth - 1):
            assert node.state.kind == "done", f"level {i} stuck in {node.state.kind}"
            assert len(node.children) == 1, f"level {i} expected 1 child, got {len(node.children)}"
            assert node.result == f"r{i}", f"level {i} result was {node.result!r}"
            node = node.children[0]
        assert node.state.kind == "done"
        assert node.result == "leaf-after-resume"


class TestTreeIndexMissingClone:
    """When a node has no clone_path (failed before snapshot resolved), its
    children's parent_file must NOT silently attribute to the grandparent's
    clone — that would misrepresent the call chain in trace output. The
    legacy `_parent_file_for` returned `root_session.file` as a "we don't
    know" sentinel; `_TreeIndex.build` must match."""

    def test_missing_clone_falls_back_to_root_not_grandparent(self, tmp_path):
        root_file = tmp_path / "root.jsonl"
        root_file.write_text("")
        a_clone = tmp_path / "a.jsonl"
        a_clone.write_text("")
        c_clone = tmp_path / "c.jsonl"
        c_clone.write_text("")

        def mk(node_id: str, clone: Path | None) -> Node:
            n = Node(
                id=node_id,
                task=node_id,
                state=st.Pending(parent_session_id="root", task=node_id),
            )
            if clone is not None:
                n.clone_path = str(clone)
            return n

        # Chain: top-level A (has clone) → B (NO clone) → C (has clone).
        # Pre-fix: C.parent_file = A.clone (grandparent — wrong).
        # Post-fix: C.parent_file = root_file (matches legacy fallback).
        c = mk("c", c_clone)
        b = mk("b", None)
        a = mk("a", a_clone)
        a.children.append(b)
        b.children.append(c)

        tree = Tree(
            root_session=SessionRef(
                session_id="00000000-0000-0000-0000-0000000000aa",
                file=root_file,
            ),
            nodes=[a],
            base_depth=0,
        )
        idx = _TreeIndex.build(tree)

        assert idx.parent_file_of[id(a)] == root_file
        assert idx.parent_file_of[id(b)] == a_clone
        assert idx.parent_file_of[id(c)] == root_file, (
            f"C's parent_file leaked to grandparent: {idx.parent_file_of[id(c)]} (expected root sentinel {root_file})"
        )


# ---------- Timeout state (PRD: don't seal report.yaml prematurely) ----------


class TestTimeoutState:
    """Timeout is the explicit terminal state recorded by
    `wait_for_terminal_signals` when the wait budget elapses without
    observing a child-side `op:return` / `op:yield` envelope. Pin its
    on-disk round-trip and status mapping so a future refactor can't
    silently collapse it back into `Failed` (which would lose the
    "we gave up vs. the child errored" distinction)."""

    def test_status_label_is_timeout(self):
        s = st.Timeout(error="budget elapsed", session_id="sess-x")
        assert st.status_label(s) == "timeout"

    def test_is_terminal_true(self):
        # Drivers, reporters, and the wait helper all branch on
        # `st.is_terminal` to decide whether to keep stepping. A
        # Timeout that isn't terminal would loop forever.
        assert st.is_terminal(st.Timeout())

    def test_round_trips_through_node_to_from_dict(self):
        node = Node(
            id="abc123ef0000000000000000000000aa",
            task="t",
            state=st.Timeout(error="elapsed", session_id="sess-x"),
            session_id="sess-x",
        )
        d = node.to_dict()
        round = Node.from_dict(d)
        assert isinstance(round.state, st.Timeout)
        assert round.state.error == "elapsed"
        assert round.state.session_id == "sess-x"
        assert round.status == "timeout"
