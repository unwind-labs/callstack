"""Tests for _run_node, run_tree, and run_resume with mocked invoke_streaming."""

import json
import os
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from callstack import (
    TreeNode,
    ExecutionTree,
    _run_node,
    run_tree,
    run_resume,
    _resume_node,
    _unwind_completed_nodes,
    _save_tree,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_args(**overrides) -> Namespace:
    defaults = dict(
        timeout=60,
        cwd="/tmp",
        model=None,
        permission_mode=None,
        max_depth=5,
        trace_dir=None,
        task=None,
        tasks=None,
        resume_session=None,
        user_reply=None,
        parent_lines=0,
    )
    defaults.update(overrides)
    return Namespace(**defaults)


def _make_tree(*nodes) -> ExecutionTree:
    return ExecutionTree(
        root_session_id="root",
        root_session_file="/tmp/root.jsonl",
        call_depth_base=1,
        nodes=list(nodes),
    )


# ---------------------------------------------------------------------------
# _run_node
# ---------------------------------------------------------------------------

class TestRunNode:

    @patch("callstack.invoke_streaming")
    @patch("callstack.resolve_session_file")
    @patch("callstack.write_trace")
    def test_complete(self, mock_trace, mock_resolve, mock_invoke, tmp_path):
        mock_invoke.return_value = ("---RETURN---\nDone!", "forked-id")
        mock_resolve.return_value = tmp_path / "forked.jsonl"

        node = TreeNode(id="n1", task="do stuff")
        tree = _make_tree(node)
        args = _make_args(trace_dir=str(tmp_path / "traces"))

        session_file = tmp_path / "parent.jsonl"
        session_file.write_text("")

        _run_node(node, session_file, args, tree, tmp_path / "traces")

        assert node.status == "complete"
        assert node.result == "Done!"
        assert node.session_id == "forked-id"

    @patch("callstack.invoke_streaming")
    @patch("callstack.resolve_session_file")
    @patch("callstack.write_trace")
    def test_yield(self, mock_trace, mock_resolve, mock_invoke, tmp_path):
        mock_invoke.return_value = ("---YIELD---\nWhat is your code?", "forked-id")
        mock_resolve.return_value = tmp_path / "forked.jsonl"

        node = TreeNode(id="n1", task="auth")
        tree = _make_tree(node)
        args = _make_args(trace_dir=str(tmp_path / "traces"))

        _run_node(node, tmp_path / "p.jsonl", args, tree, tmp_path / "traces")

        assert node.status == "yielded"
        assert node.yield_question == "What is your code?"
        assert node.yield_source == "n1"

    @patch("callstack.invoke_streaming")
    @patch("callstack.resolve_session_file")
    @patch("callstack.write_trace")
    def test_call_then_complete(self, mock_trace, mock_resolve, mock_invoke, tmp_path):
        """Agent issues ---CALL---, child completes, parent resumes and returns."""
        clone_path = tmp_path / "clone.jsonl"
        clone_path.write_text("")
        mock_resolve.return_value = clone_path

        # First call: parent says CALL
        # Second call: child returns
        # Third call: parent resumes with child result and returns
        mock_invoke.side_effect = [
            ("---CALL---\nSub-task", "parent-fork"),    # parent's first invocation
            ("---RETURN---\nChild done", "child-fork"),  # child invocation
            ("---RETURN---\nAll done", None),             # parent resume
        ]

        node = TreeNode(id="n1", task="big task")
        tree = _make_tree(node)
        args = _make_args(trace_dir=str(tmp_path / "traces"))

        _run_node(node, tmp_path / "p.jsonl", args, tree, tmp_path / "traces")

        assert node.status == "complete"
        assert node.result == "All done"
        assert len(node.children) == 1
        assert node.children[0].status == "complete"

    @patch("callstack.invoke_streaming")
    @patch("callstack.resolve_session_file")
    @patch("callstack.write_trace")
    def test_timeout_error(self, mock_trace, mock_resolve, mock_invoke, tmp_path):
        mock_invoke.side_effect = TimeoutError("timed out", "partial")

        node = TreeNode(id="n1", task="slow task")
        tree = _make_tree(node)
        args = _make_args(trace_dir=str(tmp_path / "traces"))

        _run_node(node, tmp_path / "p.jsonl", args, tree, tmp_path / "traces")

        assert node.status == "error"
        assert "timed out" in node.error

    @patch("callstack.invoke_streaming")
    @patch("callstack.resolve_session_file")
    @patch("callstack.write_trace")
    def test_invocation_exception(self, mock_trace, mock_resolve, mock_invoke, tmp_path):
        mock_invoke.side_effect = RuntimeError("CLI not found")

        node = TreeNode(id="n1", task="task")
        tree = _make_tree(node)
        args = _make_args(trace_dir=str(tmp_path / "traces"))

        _run_node(node, tmp_path / "p.jsonl", args, tree, tmp_path / "traces")

        assert node.status == "error"
        assert "Invocation failed" in node.error

    @patch("callstack.invoke_streaming")
    @patch("callstack.resolve_session_file")
    @patch("callstack.write_trace")
    def test_call_child_yields_propagates(self, mock_trace, mock_resolve, mock_invoke, tmp_path):
        """Parent calls child, child yields — yield propagates up to parent."""
        clone_path = tmp_path / "clone.jsonl"
        clone_path.write_text("")
        mock_resolve.return_value = clone_path

        mock_invoke.side_effect = [
            ("---CALL---\nNeed MFA", "parent-fork"),
            ("---YIELD---\nEnter MFA code", "child-fork"),
        ]

        node = TreeNode(id="n1", task="auth flow")
        tree = _make_tree(node)
        args = _make_args(trace_dir=str(tmp_path / "traces"))

        _run_node(node, tmp_path / "p.jsonl", args, tree, tmp_path / "traces")

        assert node.status == "yielded"
        assert node.yield_question == "Enter MFA code"
        # yield_source should point to child, not self
        child = node.children[0]
        assert node.yield_source == child.id
        assert child.status == "yielded"


# ---------------------------------------------------------------------------
# _resume_node
# ---------------------------------------------------------------------------

class TestResumeNode:

    @patch("callstack.invoke_streaming")
    @patch("callstack.write_trace")
    def test_resume_completes(self, mock_trace, mock_invoke, tmp_path):
        mock_invoke.return_value = ("---RETURN---\nResumed result", None)

        node = TreeNode(
            id="n1", task="auth", session_id="sess-1",
            clone_path=str(tmp_path / "clone.jsonl"),
            status="yielded", yield_source="n1",
        )
        tree = _make_tree(node)
        args = _make_args(trace_dir=str(tmp_path / "traces"))

        _resume_node(node, "847291", args, tree, tmp_path / "traces")

        assert node.status == "complete"
        assert node.result == "Resumed result"
        assert node.yield_question is None

    @patch("callstack.invoke_streaming")
    @patch("callstack.write_trace")
    def test_resume_yields_again(self, mock_trace, mock_invoke, tmp_path):
        mock_invoke.return_value = ("---YIELD---\nNow enter password", None)

        node = TreeNode(
            id="n1", task="auth", session_id="sess-1",
            clone_path=str(tmp_path / "clone.jsonl"),
            status="yielded", yield_source="n1",
        )
        tree = _make_tree(node)
        args = _make_args(trace_dir=str(tmp_path / "traces"))

        _resume_node(node, "code123", args, tree, tmp_path / "traces")

        assert node.status == "yielded"
        assert node.yield_question == "Now enter password"


# ---------------------------------------------------------------------------
# _unwind_completed_nodes
# ---------------------------------------------------------------------------

class TestUnwindCompletedNodes:

    @patch("callstack.invoke_streaming")
    @patch("callstack.write_trace")
    def test_parent_unblocked_when_child_completes(self, mock_trace, mock_invoke, tmp_path):
        """Parent was yielded waiting on child. Child is now complete. Unwind resumes parent."""
        mock_invoke.return_value = ("---RETURN---\nParent done", None)

        child = TreeNode(
            id="c1", task="sub", status="complete", result="child result",
            session_id="child-sess", clone_path=str(tmp_path / "child.jsonl"),
        )
        parent = TreeNode(
            id="p1", task="main", status="yielded",
            yield_source="c1",  # blocked on child
            session_id="parent-sess",
            clone_path=str(tmp_path / "parent.jsonl"),
            children=[child],
        )
        tree = _make_tree(parent)
        args = _make_args(trace_dir=str(tmp_path / "traces"))

        _unwind_completed_nodes(tree, args, tmp_path / "traces")

        assert parent.status == "complete"
        assert parent.result == "Parent done"


# ---------------------------------------------------------------------------
# run_tree
# ---------------------------------------------------------------------------

class TestRunTree:

    @patch("callstack.invoke_streaming")
    @patch("callstack.resolve_session_file")
    @patch("callstack.write_trace")
    def test_single_task(self, mock_trace, mock_resolve, mock_invoke, tmp_path):
        mock_invoke.return_value = ("---RETURN---\nResult", "fork-1")
        mock_resolve.return_value = tmp_path / "fork.jsonl"

        session_file = tmp_path / "session.jsonl"
        session_file.write_text("")
        args = _make_args(task="single task", trace_dir=str(tmp_path / "traces"))

        result = json.loads(run_tree(args, session_file, "sess-id", 1))
        assert result["status"] == "complete"
        assert result["result"] == "Result"

    @patch("callstack.invoke_streaming")
    @patch("callstack.resolve_session_file")
    @patch("callstack.write_trace")
    def test_parallel_tasks(self, mock_trace, mock_resolve, mock_invoke, tmp_path):
        mock_invoke.return_value = ("---RETURN---\nDone", "fork-1")
        mock_resolve.return_value = tmp_path / "fork.jsonl"

        session_file = tmp_path / "session.jsonl"
        session_file.write_text("")
        args = _make_args(tasks=["task A", "task B"], trace_dir=str(tmp_path / "traces"))

        result = json.loads(run_tree(args, session_file, "sess-id", 1))
        assert isinstance(result, list)
        assert len(result) == 2
        assert all(r["result"] == "Done" for r in result)

    @patch("callstack.invoke_streaming")
    @patch("callstack.resolve_session_file")
    @patch("callstack.write_trace")
    def test_single_task_yield(self, mock_trace, mock_resolve, mock_invoke, tmp_path):
        clone_path = tmp_path / "clone.jsonl"
        clone_path.write_text("")
        mock_invoke.return_value = ("---YIELD---\nNeed input", "fork-1")
        mock_resolve.return_value = clone_path

        session_file = tmp_path / "session.jsonl"
        session_file.write_text("")
        args = _make_args(task="interactive task", trace_dir=str(tmp_path / "traces"))

        result = json.loads(run_tree(args, session_file, "sess-id", 1))
        assert result["status"] == "yield"
        assert result["question"] == "Need input"


# ---------------------------------------------------------------------------
# run_resume
# ---------------------------------------------------------------------------

class TestRunResume:

    @patch("callstack.invoke_streaming")
    @patch("callstack.resolve_session_file")
    @patch("callstack.write_trace")
    def test_resume_no_tree(self, mock_trace, mock_resolve, mock_invoke, tmp_path):
        """Resume without a saved tree (backward compat path)."""
        clone = tmp_path / "clone.jsonl"
        clone.write_text("")
        mock_resolve.return_value = clone
        mock_invoke.return_value = ("---RETURN---\nFinal", None)

        args = _make_args(
            resume_session="clone",
            user_reply="847291",
            trace_dir=str(tmp_path / "traces"),
        )
        result = json.loads(run_resume(args))
        assert result["status"] == "complete"
        assert result["result"] == "Final"

    @patch("callstack.invoke_streaming")
    @patch("callstack.resolve_session_file")
    @patch("callstack.write_trace")
    def test_resume_with_tree(self, mock_trace, mock_resolve, mock_invoke, tmp_path):
        """Resume with a saved execution tree."""
        clone = tmp_path / "clone.jsonl"
        clone.write_text("")

        # Save a tree with a yielded node
        tree = ExecutionTree(
            root_session_id="root",
            root_session_file="/tmp/root.jsonl",
            call_depth_base=1,
            nodes=[TreeNode(
                id="n1", task="auth", session_id="clone",
                clone_path=str(clone), status="yielded",
                yield_question="Enter code", yield_source="n1",
            )],
        )
        _save_tree(tree, clone)

        mock_resolve.return_value = clone
        mock_invoke.return_value = ("---RETURN---\nAuthenticated", None)

        args = _make_args(
            resume_session="clone",
            user_reply="847291",
            trace_dir=str(tmp_path / "traces"),
        )
        result = json.loads(run_resume(args))
        assert result["status"] == "complete"
        assert result["result"] == "Authenticated"

    @patch("callstack.resolve_session_file")
    def test_resume_missing_session(self, mock_resolve, tmp_path):
        mock_resolve.return_value = None
        args = _make_args(
            resume_session="nonexistent",
            user_reply="reply",
        )
        result = json.loads(run_resume(args))
        assert result["status"] == "error"
        assert "Cannot find" in result["error"]
