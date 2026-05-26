"""Tests for ClaudeChannel._build_cmd: assert the right claude CLI flags are
emitted for each turn mode (fork / fresh / resume)."""
from __future__ import annotations

import io

import pytest

from agent_callstack.channel import ClaudeChannel, _fire_on_session_id


@pytest.fixture
def channel() -> ClaudeChannel:
    return ClaudeChannel(model="opus")


class TestBuildCmd:
    def test_fork_uses_resume_and_fork_session(self, channel):
        cmd = channel._build_cmd("parent-sid", "fork")
        assert "--resume" in cmd
        assert "parent-sid" in cmd
        assert "--fork-session" in cmd

    def test_fresh_omits_resume_and_fork_session(self, channel):
        cmd = channel._build_cmd("ignored", "fresh")
        assert "--resume" not in cmd
        assert "--fork-session" not in cmd
        # Still has the core invariants.
        assert "claude" in cmd[0]
        assert "stream-json" in cmd

    def test_resume_uses_resume_only(self, channel):
        cmd = channel._build_cmd("sid-123", "resume")
        assert "--resume" in cmd
        assert "sid-123" in cmd
        assert "--fork-session" not in cmd

    def test_model_flag_propagates(self, channel):
        cmd = channel._build_cmd("p", "fork")
        i = cmd.index("--model")
        assert cmd[i + 1] == "opus"

    def test_no_model_means_no_model_flag(self):
        cmd = ClaudeChannel()._build_cmd("p", "fresh")
        assert "--model" not in cmd


class TestFireOnSessionId:
    """R-M1: ClaudeChannel and ScriptedChannel share one helper for invoking
    the advisory on_session_id callback so the exception-handling can't drift.
    The helper logs a raise to stderr, plus the per-turn log when given, and
    never propagates into the turn (SEC-011)."""

    def test_success_does_not_log(self, capsys):
        log = io.StringIO()
        seen: list[str] = []
        _fire_on_session_id(seen.append, "sid-1", log)
        assert seen == ["sid-1"]
        assert log.getvalue() == ""
        assert capsys.readouterr().err == ""

    def test_raise_surfaces_to_both_stderr_and_log(self, capsys):
        log = io.StringIO()

        def boom(_sid: str) -> None:
            raise ValueError("nope")

        # Must not propagate — advisory observer.
        _fire_on_session_id(boom, "sid-1", log)
        logged = log.getvalue()
        err = capsys.readouterr().err
        assert "ValueError" in logged and "nope" in logged
        assert "ValueError" in err and "nope" in err

    def test_raise_without_log_still_hits_stderr(self, capsys):
        def boom(_sid: str) -> None:
            raise RuntimeError("kaboom")

        # ScriptedChannel passes no log sink — stderr only, no crash.
        _fire_on_session_id(boom, "sid-1")
        assert "RuntimeError" in capsys.readouterr().err
