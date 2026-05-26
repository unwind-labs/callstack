"""Tests for the SessionAnalyzer + format helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent_callstack.analysis import (
    SessionAnalyzer,
    _content_preview,
    _parse_ts,
    format_duration,
    format_size,
    format_tree,
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
        f = _write_trace(
            trace_dir,
            {
                "timestamp": "2026-04-16T10:00:00.000",
                "call_depth": 1,
                "session_id": "s1",
                "task": "do thing",
                "duration_seconds": 1.5,
                "result_length": 42,
                "error": None,
            },
        )
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
        path.write_text(
            '{"valid": "json", "session_id": "s"}\nnot json at all\n{"valid": "again", "session_id": "s2"}\n'
        )
        events = SessionAnalyzer().trace_events(path)
        assert len(events) == 2


class TestSessionStats:
    def test_counts_messages_by_type(self, trace_dir):
        _write_session(
            trace_dir,
            "s1",
            {"type": "user", "timestamp": "2026-04-16T10:00:00", "message": {"role": "user", "content": "hi"}},
            {
                "type": "assistant",
                "timestamp": "2026-04-16T10:00:01",
                "message": {"role": "assistant", "content": "hello"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-04-16T10:00:02",
                "message": {"role": "assistant", "content": "again"},
            },
        )
        stats = SessionAnalyzer().session_stats(trace_dir / "s1.jsonl")
        assert stats.message_count == 3
        assert stats.by_type == {"user": 1, "assistant": 2}
        assert stats.duration == 2.0

    def test_streams_without_materializing_messages(self, trace_dir, monkeypatch):
        """R-M2: a session JSONL can be many MB. session_stats only needs
        per-message type + timestamp, so it must stream the file and accumulate
        in O(1) message memory — NOT route through session_messages(), which
        builds the full SessionMessage list. Pin that it does not call it."""
        _write_session(
            trace_dir,
            "s1",
            {"type": "user", "timestamp": "2026-04-16T10:00:00"},
            {"type": "assistant", "timestamp": "2026-04-16T10:00:05"},
        )
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
        assert not called, "session_stats must stream, not materialize via session_messages"

    def test_duplicate_timestamps_do_not_widen_window(self, trace_dir):
        """Duration is last-minus-first; messages sharing a timestamp must
        leave the min/max window unchanged (neither earlier nor later), so a
        burst of same-instant messages doesn't inflate the reported duration."""
        _write_session(
            trace_dir,
            "s1",
            {"type": "user", "timestamp": "2026-04-16T10:00:00"},
            {"type": "assistant", "timestamp": "2026-04-16T10:00:00"},
            {"type": "assistant", "timestamp": "2026-04-16T10:00:00"},
        )
        stats = SessionAnalyzer().session_stats(trace_dir / "s1.jsonl")
        assert stats.message_count == 3
        assert stats.duration == 0.0

    def test_missing_file_yields_empty_stats(self, tmp_path):
        """R-M2: streaming reader on an absent file -> zeroed stats, not error."""
        stats = SessionAnalyzer().session_stats(tmp_path / "ghost.jsonl")
        assert stats.message_count == 0
        assert stats.by_type == {}
        assert stats.duration == 0.0

    def test_untimestamped_and_blank_lines_are_tolerated(self, trace_dir):
        """Live logs interleave blank lines and messages with no/invalid
        timestamp. Blank lines are skipped by the reader; untimestamped
        messages still count toward message_count but don't move the time
        window. The duration here comes only from the two real timestamps."""
        path = trace_dir / "s1.jsonl"
        path.write_text(
            '{"type": "system"}\n'  # no timestamp
            "\n"  # blank line
            '{"type": "user", "timestamp": "2026-04-16T10:00:00"}\n'
            '{"type": "assistant", "timestamp": "not-a-date"}\n'  # unparseable ts
            '{"type": "assistant", "timestamp": "2026-04-16T10:00:03"}\n'
        )
        stats = SessionAnalyzer().session_stats(path)
        assert stats.message_count == 4
        assert stats.duration == 3.0


class TestSessionMessages:
    """session_messages() is the read path the inspect CLI renders rows from;
    each message's flat text/tool preview is what an operator actually reads,
    so a wrong preview = wrong displayed trace."""

    def test_extracts_role_text_and_tool(self, trace_dir):
        _write_session(
            trace_dir,
            "s1",
            {"type": "user", "timestamp": "2026-04-16T10:00:00", "message": {"role": "user", "content": "hello there"}},
            {
                "type": "assistant",
                "timestamp": "2026-04-16T10:00:01",
                "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash"}]},
            },
        )
        msgs = SessionAnalyzer().session_messages(trace_dir / "s1.jsonl")
        assert [m.role for m in msgs] == ["user", "assistant"]
        assert msgs[0].text == "hello there"
        assert msgs[0].tool_name is None
        assert msgs[1].tool_name == "Bash"

    def test_role_none_when_message_absent(self, trace_dir):
        """System lines often carry no `message` field at all — role must fall
        back to None (not raise) so those lines still render as rows."""
        _write_session(trace_dir, "s1", {"type": "system", "timestamp": "2026-04-16T10:00:00"})
        msgs = SessionAnalyzer().session_messages(trace_dir / "s1.jsonl")
        assert len(msgs) == 1
        assert msgs[0].role is None
        assert msgs[0].text == ""


class TestContentPreview:
    """_content_preview turns a raw message into (text, tool_name). It's the
    single point that decides what shows for every message shape Claude Code
    emits; an unhandled shape would silently blank out trace rows."""

    def test_plain_string_content(self):
        assert _content_preview({"message": {"content": "hi"}}) == ("hi", None)

    def test_text_block_in_list(self):
        obj = {"message": {"content": [{"type": "text", "text": "block text"}]}}
        assert _content_preview(obj) == ("block text", None)

    def test_tool_use_block_returns_tool_name(self):
        obj = {"message": {"content": [{"type": "tool_use", "name": "Read"}]}}
        assert _content_preview(obj) == ("", "Read")

    def test_tool_result_string_content(self):
        obj = {"message": {"content": [{"type": "tool_result", "content": "output"}]}}
        assert _content_preview(obj) == ("output", None)

    def test_tool_result_non_string_content(self):
        """tool_result content can be a list of blocks (not a str); preview
        must degrade to empty text, never index into a non-str."""
        obj = {"message": {"content": [{"type": "tool_result", "content": [{"type": "text", "text": "x"}]}]}}
        assert _content_preview(obj) == ("", None)

    def test_non_dict_block_is_skipped(self):
        """Mixed list with a stray non-dict entry before the real block must
        skip the junk, not crash."""
        obj = {"message": {"content": ["junk", {"type": "text", "text": "real"}]}}
        assert _content_preview(obj) == ("real", None)

    def test_unknown_block_type_falls_through(self):
        obj = {"message": {"content": [{"type": "image", "source": "..."}]}}
        assert _content_preview(obj) == ("", None)

    def test_missing_message_returns_empty(self):
        assert _content_preview({}) == ("", None)

    def test_non_dict_message_does_not_crash(self):
        """A truthy non-dict `message` (e.g. a JSON string) must degrade to
        empty, not raise: `or {}` only catches falsy values, so without an
        isinstance guard msg.get(...) would AttributeError on a str."""
        assert _content_preview({"message": "just a string"}) == ("", None)

    def test_dict_message_without_content_falls_through(self):
        """A dict message whose content is absent (neither str nor list) must
        degrade to empty via the final fallthrough — exercised separately from
        the missing-message case, which now short-circuits at the dict guard."""
        assert _content_preview({"message": {"role": "user"}}) == ("", None)

    def test_text_is_truncated_to_200_chars(self):
        long = "x" * 500
        text, _ = _content_preview({"message": {"content": long}})
        assert len(text) == 200


class TestParseTs:
    def test_bad_timestamp_returns_none(self):
        """_parse_ts must never raise on garbage — a malformed ts in one line
        can't be allowed to abort reading the whole log."""
        assert _parse_ts("not-a-date") is None

    def test_empty_returns_none(self):
        assert _parse_ts(None) is None
        assert _parse_ts("") is None

    def test_parses_z_suffix_as_utc(self):
        ts = _parse_ts("2026-04-16T10:00:00Z")
        assert ts is not None
        assert ts.utcoffset().total_seconds() == 0


class TestParentResolution:
    def test_finds_parent_in_metadata(self, trace_dir):
        _write_session(trace_dir, "child", parent="parent-id")
        assert SessionAnalyzer().parent_session_id(trace_dir / "child.jsonl") == "parent-id"

    def test_returns_none_if_no_parent(self, trace_dir):
        _write_session(trace_dir, "root", {"type": "user"})
        assert SessionAnalyzer().parent_session_id(trace_dir / "root.jsonl") is None

    def test_missing_file_returns_none(self, tmp_path):
        assert SessionAnalyzer().parent_session_id(tmp_path / "ghost.jsonl") is None

    def test_skips_blank_and_unparseable_lines(self, trace_dir):
        """Live session logs may have blank/torn lines before the metadata
        line — the parent scan must skip them, not give up at the first."""
        path = trace_dir / "s1.jsonl"
        path.write_text('\nnot json\n{"type": "system"}\n{"parentSessionId": "p1"}\n')
        assert SessionAnalyzer().parent_session_id(path) == "p1"

    def test_stops_scanning_after_50_lines(self, trace_dir):
        """The parent id is early metadata; the scan caps at ~50 lines so a
        huge session log isn't walked end-to-end. A parent buried past the cap
        is intentionally not found."""
        path = trace_dir / "s1.jsonl"
        lines = [json.dumps({"type": "user"}) for _ in range(60)]
        lines.append(json.dumps({"parentSessionId": "late"}))
        path.write_text("\n".join(lines) + "\n")
        assert SessionAnalyzer().parent_session_id(path) is None


class TestBuildTree:
    def test_two_node_tree(self, trace_dir):
        f = _write_trace(
            trace_dir,
            {"session_id": "root", "task": "main", "duration_seconds": 2.0},
            {"session_id": "child", "task": "sub", "duration_seconds": 1.0},
        )
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

    def test_no_root_when_every_session_has_in_tree_parent(self, trace_dir):
        """If parent links form a cycle (every session's parent is itself in
        the trace), there is no root to anchor on — build_tree must return None
        rather than loop or pick arbitrarily."""
        f = _write_trace(
            trace_dir,
            {"session_id": "a", "task": "A", "duration_seconds": 1.0},
            {"session_id": "b", "task": "B", "duration_seconds": 1.0},
        )
        _write_session(trace_dir, "a", parent="b")
        _write_session(trace_dir, "b", parent="a")
        assert SessionAnalyzer().build_tree(f, root_session=None) is None


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

        root = CallNode(
            session_id="root123",
            task="Main task",
            depth=0,
            duration=2.5,
            children=[CallNode(session_id="child123", task="sub", depth=1, duration=1.0)],
        )
        out = format_tree(root)
        assert "root123" in out
        assert "child123" in out
        assert "Main task" in out
        assert "2.5s" in out
