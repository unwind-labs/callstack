"""Tests for ClaudeChannel._build_cmd: assert the right claude CLI flags are
emitted for each turn mode (fork / fresh / resume)."""
from __future__ import annotations

import pytest

from agent_callstack.channel import ClaudeChannel


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
