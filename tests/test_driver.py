"""Driver tests with ScriptedChannel — exercises the full state machine end-to-end
without spawning any subprocess."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_callstack.channel import ScriptedChannel, TurnResult, TurnTimeout
from agent_callstack.driver import Driver, Tree
from agent_callstack.session import SessionLocator, SessionRef
from agent_callstack.trace import TraceWriter, TreeStore


# ---------- helpers ----------

def _envelope(op: str, **fields) -> str:
    return "```json\n" + json.dumps({"op": op, **fields}) + "\n```"


@pytest.fixture
def parent_session(tmp_path) -> SessionRef:
    f = tmp_path / "parent.jsonl"
    f.write_text("line1\nline2\nline3\n")
    return SessionRef(session_id="parent-id", file=f)


def _make_driver(tmp_path, channel: ScriptedChannel, *, max_depth: int = 5) -> Driver:
    return Driver(
        channel=channel,
        locator=SessionLocator(projects_dir=tmp_path / "_no_real_projects"),
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
        assert ch.log[0][2] is True

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
        ch = (ScriptedChannel()
              .respond(_envelope("call", task="sub-task"), "parent-fork")
              .respond(_envelope("return", result="sub-result"), "child-fork")
              .respond(_envelope("return", result="all done"), "parent-fork"))
        driver = _make_driver(tmp_path, ch)

        tree = driver.run(parent_session, ["main"])

        root = tree.nodes[0]
        assert root.status == "complete"
        assert root.result == "all done"
        assert len(root.children) == 1
        assert root.children[0].result == "sub-result"
        # Parent's resume must be NON-fork; child's fork is True.
        forks = [entry[2] for entry in ch.log]
        assert forks == [True, True, False]

    def test_child_yield_propagates_pause(self, tmp_path, parent_session):
        """Child yields → tree is paused; parent stays in awaiting_child."""
        ch = (ScriptedChannel()
              .respond(_envelope("call", task="sub"), "parent-fork")
              .respond(_envelope("yield", question="Code?"), "child-fork"))
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


# ---------- parallel root tasks ----------

class TestParallel:

    def test_two_tasks_complete(self, tmp_path, parent_session):
        # Order across threads is non-deterministic; key on the unique tail of the
        # prompt (which always ends with the task text after a "\n\n").
        marker_to_response = {
            "task ALPHA": _envelope("return", result="ALPHA done"),
            "task BRAVO": _envelope("return", result="BRAVO done"),
        }
        def respond(_src, prompt, _fork):
            tail = prompt.rsplit("\n\n", 1)[-1]
            body = marker_to_response.get(tail)
            if body is None:
                raise AssertionError(f"unrecognized task tail: {tail!r}")
            sid = "alpha-fork" if "ALPHA" in tail else "bravo-fork"
            return TurnResult(text=body, session_id=sid, duration=0.0)
        ch = ScriptedChannel().respond_with(respond).respond_with(respond)
        driver = _make_driver(tmp_path, ch)

        tree = driver.run(parent_session, ["task ALPHA", "task BRAVO"])

        assert len(tree.nodes) == 2
        assert {n.result for n in tree.nodes} == {"ALPHA done", "BRAVO done"}
        assert all(n.status == "complete" for n in tree.nodes)


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
        # Prepare a clone path the locator can resolve (must live under a
        # project subdir, since SessionLocator iterates projects_dir/*).
        project_dir = tmp_path / "_no_real_projects" / "fake-project"
        project_dir.mkdir(parents=True)
        clone_path = project_dir / "child-fork.jsonl"
        clone_path.write_text("")

        ch = ScriptedChannel().respond(_envelope("yield", question="Code?"), "child-fork")
        driver = _make_driver(tmp_path, ch)
        tree = driver.run(parent_session, ["auth"])
        leaf = tree.yielded_leaves()[0]
        assert Path(leaf.clone_path or "") == clone_path

        # Sidecar was saved.
        store = TreeStore()
        snapshot = store.load(clone_path)
        assert snapshot is not None
        loaded = Tree.from_dict(snapshot)
        assert loaded.yielded_leaves()[0].session_id == "child-fork"

        # Resume the leaf.
        ch.respond(_envelope("return", result="authenticated"), "child-fork")
        driver.resume(loaded, target_session_id="child-fork", reply="847291")

        leaf2 = loaded.nodes[0]
        assert leaf2.status == "complete"
        assert leaf2.result == "authenticated"
        # The resume turn must NOT be a fork.
        last_call = ch.log[-1]
        assert last_call[2] is False
        assert last_call[1] == "847291"

    def test_resume_unblocks_parent(self, tmp_path, parent_session):
        """Child yields, blocking parent. After child resume, parent is unblocked."""
        project_dir = tmp_path / "_no_real_projects" / "fake-project"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "parent-fork.jsonl").write_text("")
        (project_dir / "child-fork.jsonl").write_text("")

        ch = (ScriptedChannel()
              .respond(_envelope("call", task="sub"), "parent-fork")
              .respond(_envelope("yield", question="MFA?"), "child-fork"))
        driver = _make_driver(tmp_path, ch)
        tree = driver.run(parent_session, ["main"])
        root, child = tree.nodes[0], tree.nodes[0].children[0]
        assert child.status == "yielded"
        assert root.status == "running"

        # Resume the child: it returns, parent should resume and complete.
        ch.respond(_envelope("return", result="auth-ok"), "child-fork")
        ch.respond(_envelope("return", result="all done"), "parent-fork")
        driver.resume(tree, target_session_id="child-fork", reply="847291")

        assert child.status == "complete"
        assert root.status == "complete"
        assert root.result == "all done"
