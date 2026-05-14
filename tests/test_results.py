"""Tests for results._find_task_start_line."""
from __future__ import annotations

from agent_callstack.results import _find_task_start_line


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
