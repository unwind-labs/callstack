"""Tests for the SessionAnalyzer + format helpers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_callstack.analysis import (
    SessionAnalyzer, format_duration, format_size, format_tree,
)


@pytest.fixture
def trace_dir(tmp_path) -> Path:
    d = tmp_path / "call_traces"
    d.mkdir()
    return d


def _write_trace(trace_dir: Path, *entries: dict) -> Path:
    path = trace_dir / "call_trace.jsonl"
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return path


def _write_session(trace_dir: Path, sid: str, *messages: dict, parent: str | None = None):
    path = trace_dir / f"{sid}.jsonl"
    if parent:
        first = {"parentSessionId": parent, "type": "system"}
        with open(path, "w") as f:
            f.write(json.dumps(first) + "\n")
            for m in messages:
                f.write(json.dumps(m) + "\n")
    else:
        with open(path, "w") as f:
            for m in messages:
                f.write(json.dumps(m) + "\n")


class TestTraceEvents:

    def test_parses_entries(self, trace_dir):
        f = _write_trace(trace_dir,
                         {"timestamp": "2026-04-16T10:00:00.000",
                          "call_depth": 1, "session_id": "s1",
                          "task": "do thing", "duration_seconds": 1.5,
                          "result_length": 42, "error": None})
        events = SessionAnalyzer().trace_events(f)
        assert len(events) == 1
        e = events[0]
        assert e.session_id == "s1"
        assert e.task == "do thing"
        assert e.duration == 1.5
        assert e.depth == 1
        assert e.result_length == 42

    def test_missing_file_returns_empty(self, tmp_path):
        assert SessionAnalyzer().trace_events(tmp_path / "ghost.jsonl") == []

    def test_skips_unparseable_lines(self, trace_dir):
        path = trace_dir / "call_trace.jsonl"
        path.write_text('{"valid": "json", "session_id": "s"}\n'
                        'not json at all\n'
                        '{"valid": "again", "session_id": "s2"}\n')
        events = SessionAnalyzer().trace_events(path)
        assert len(events) == 2


class TestSessionStats:

    def test_counts_messages_by_type(self, trace_dir):
        _write_session(trace_dir, "s1",
                       {"type": "user", "timestamp": "2026-04-16T10:00:00",
                        "message": {"role": "user", "content": "hi"}},
                       {"type": "assistant", "timestamp": "2026-04-16T10:00:01",
                        "message": {"role": "assistant", "content": "hello"}},
                       {"type": "assistant", "timestamp": "2026-04-16T10:00:02",
                        "message": {"role": "assistant", "content": "again"}})
        stats = SessionAnalyzer().session_stats(trace_dir / "s1.jsonl")
        assert stats.message_count == 3
        assert stats.by_type == {"user": 1, "assistant": 2}
        assert stats.duration == 2.0

    def test_streams_without_materializing_messages(self, trace_dir, monkeypatch):
        """R-M2: a session JSONL can be many MB. session_stats only needs
        per-message type + timestamp, so it must stream the file and accumulate
        in O(1) message memory — NOT route through session_messages(), which
        builds the full SessionMessage list. Pin that it does not call it."""
        _write_session(trace_dir, "s1",
                       {"type": "user", "timestamp": "2026-04-16T10:00:00"},
                       {"type": "assistant", "timestamp": "2026-04-16T10:00:05"})
        analyzer = SessionAnalyzer()
        called = False
        orig = analyzer.session_messages

        def spy(*a, **k):
            nonlocal called
            called = True
            return orig(*a, **k)

        monkeypatch.setattr(analyzer, "session_messages", spy)
        stats = analyzer.session_stats(trace_dir / "s1.jsonl")
        assert stats.message_count == 2
        assert stats.duration == 5.0
        assert not called, (
            "session_stats must stream, not materialize via session_messages")

    def test_missing_file_yields_empty_stats(self, tmp_path):
        """R-M2: streaming reader on an absent file -> zeroed stats, not error."""
        stats = SessionAnalyzer().session_stats(tmp_path / "ghost.jsonl")
        assert stats.message_count == 0
        assert stats.by_type == {}
        assert stats.duration == 0.0


class TestParentResolution:

    def test_finds_parent_in_metadata(self, trace_dir):
        _write_session(trace_dir, "child", parent="parent-id")
        assert SessionAnalyzer().parent_session_id(trace_dir / "child.jsonl") == "parent-id"

    def test_returns_none_if_no_parent(self, trace_dir):
        _write_session(trace_dir, "root", {"type": "user"})
        assert SessionAnalyzer().parent_session_id(trace_dir / "root.jsonl") is None


class TestBuildTree:

    def test_two_node_tree(self, trace_dir):
        f = _write_trace(trace_dir,
                         {"session_id": "root", "task": "main", "duration_seconds": 2.0},
                         {"session_id": "child", "task": "sub", "duration_seconds": 1.0})
        _write_session(trace_dir, "root", {"type": "user"})
        _write_session(trace_dir, "child", parent="root")

        tree = SessionAnalyzer().build_tree(f, root_session="root")
        assert tree is not None
        assert tree.session_id == "root"
        assert len(tree.children) == 1
        assert tree.children[0].session_id == "child"
        assert tree.children[0].depth == 1
        assert tree.duration == 2.0

    def test_no_events_returns_none(self, trace_dir):
        f = trace_dir / "call_trace.jsonl"
        f.write_text("")
        assert SessionAnalyzer().build_tree(f) is None


class TestFormatHelpers:

    def test_duration_units(self):
        assert format_duration(0.05) == "50ms"
        assert format_duration(0.5) == "0.50s"
        assert format_duration(5.0) == "5.0s"
        assert format_duration(90.0) == "1m30s"

    def test_size(self):
        assert format_size(500) == "500B"
        assert format_size(2048) == "2KB"

    def test_tree_render(self):
        from agent_callstack.analysis import CallNode
        root = CallNode(session_id="root123", task="Main task", depth=0, duration=2.5,
                        children=[CallNode(session_id="child123", task="sub",
                                           depth=1, duration=1.0)])
        out = format_tree(root)
        assert "root123" in out
        assert "child123" in out
        assert "Main task" in out
        assert "2.5s" in out
