"""Tests for results._find_task_start_line and node→result translation."""

from __future__ import annotations

from agent_callstack import state as st
from agent_callstack.driver import Node
from agent_callstack.results import CallFailed, _find_task_start_line, _result_from_node


def test_finds_marker_returns_last_occurrence(tmp_path):
    # The CLI writes `## Starting Task [<id>]` twice: once in the
    # queue-operation bookkeeping row near the top, then again as the
    # real user message after the inherited transcript replays. We want
    # the last occurrence — that's where the model actually receives
    # the task and the child's own conversation begins.
    f = tmp_path / "fork.jsonl"
    f.write_text(
        "queue-op-with: ## Starting Task [abc12345] in content\n"
        "inherited line\n"
        "inherited line\n"
        "user message: ## Starting Task [abc12345] for real\n"
        "assistant: doing it\n"
    )
    assert _find_task_start_line(f, "abc12345") == 4


def test_marker_absent_returns_none(tmp_path):
    f = tmp_path / "fork.jsonl"
    f.write_text("nothing relevant\nstill nothing\n")
    assert _find_task_start_line(f, "abc12345") is None


def test_missing_file_returns_none(tmp_path):
    assert _find_task_start_line(tmp_path / "nope.jsonl", "abc12345") is None


def test_does_not_match_different_task_id(tmp_path):
    # Nested forks replay parent's `## Starting Task [<other-id>]` markers
    # into the child's JSONL. Only matches for our own task_id should
    # influence the result.
    f = tmp_path / "fork.jsonl"
    f.write_text(
        "## Starting Task [othernode] inherited from parent\n"
        "## Starting Task [abc12345] my own task\n"
        "## Starting Task [grandkid] from a deeper replay\n"
    )
    assert _find_task_start_line(f, "abc12345") == 2


# ---------- _result_from_node: terminal-state translation ----------


def test_timeout_node_surfaces_real_error_not_unexpected_state():
    # WHY this isn't hypothetical: report.seal() runs
    # terminal_wait.expire_to_timeout BEFORE the Caller extracts results
    # (__init__.py ~224/274), so a top-level node still waiting for a late
    # terminal envelope is stamped Timeout and is exactly what
    # _result_from_node sees. It must surface the timeout's own message so the
    # caller learns WHY the call ended, not a misleading "unexpected state:
    # timeout" that drops the real reason.
    state = st.Timeout()  # default error: "wait-for-terminal-envelope budget elapsed"
    node = Node(id="n1", task="t", state=state, error=state.error)  # error mirrored by _denormalize
    out = _result_from_node(node)
    assert isinstance(out, CallFailed)
    assert out.error == "wait-for-terminal-envelope budget elapsed"
    assert "unexpected state" not in out.error


def test_abandoned_node_surfaces_abandon_reason():
    # WHY this isn't hypothetical: orphan reconciliation (crashed writer pid)
    # and shutdown hardening (atexit/SIGTERM/SIGINT) both stamp Abandoned on
    # in-flight nodes that are then read out as results. The abandon reason
    # carries the only diagnostic the caller will ever get about the seal.
    state = st.Abandoned(error="writer pid 4242 no longer alive")
    node = Node(id="n2", task="t", state=state, error=state.error)
    out = _result_from_node(node)
    assert isinstance(out, CallFailed)
    assert out.error == "writer pid 4242 no longer alive"


def test_terminal_error_falls_back_to_state_when_node_error_unset():
    # Defensive: even if denormalization hasn't mirrored s.error onto
    # node.error, the state's own error is the source of truth and must still
    # reach the caller rather than collapsing to None.
    node = Node(id="n3", task="t", state=st.Abandoned(error="sealed at shutdown"))
    assert node.error is None
    out = _result_from_node(node)
    assert isinstance(out, CallFailed)
    assert out.error == "sealed at shutdown"
