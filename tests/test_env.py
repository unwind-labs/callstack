"""Tests for `agent_callstack.env`: single source of truth for the
runtime's env-var names and parsing policy.

These tests pin the typed readers' default/fallback policy and the
defensive ceilings so a future change can't silently widen them.
"""

from __future__ import annotations

import pytest
from agent_callstack import env


class TestMaxDepth:
    def test_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv(env.ENV_MAX_DEPTH, raising=False)
        assert env.max_depth() == 10

    def test_explicit_legitimate_value(self, monkeypatch):
        monkeypatch.setenv(env.ENV_MAX_DEPTH, "5")
        assert env.max_depth() == 5

    def test_huge_value_clamped_to_ceiling(self, monkeypatch):
        # SEC-103: defensive ceiling at 32 — depth-bombs can't OOM the
        # host even when the env says 1M.
        monkeypatch.setenv(env.ENV_MAX_DEPTH, "1000000")
        assert env.max_depth() == 32

    @pytest.mark.parametrize("bad", ["0", "-1", "abc", ""])
    def test_invalid_returns_default(self, bad, monkeypatch):
        monkeypatch.setenv(env.ENV_MAX_DEPTH, bad)
        assert env.max_depth() == 10


class TestMaxFanout:
    def test_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv(env.ENV_MAX_FANOUT, raising=False)
        assert env.max_fanout() == 64

    def test_explicit_value(self, monkeypatch):
        monkeypatch.setenv(env.ENV_MAX_FANOUT, "16")
        assert env.max_fanout() == 16

    @pytest.mark.parametrize("bad", ["0", "-1", "abc"])
    def test_invalid_returns_default(self, bad, monkeypatch):
        monkeypatch.setenv(env.ENV_MAX_FANOUT, bad)
        assert env.max_fanout() == 64


class TestReportDebounceSecs:
    def test_default(self, monkeypatch):
        monkeypatch.delenv(env.ENV_REPORT_DEBOUNCE_SECS, raising=False)
        assert env.report_debounce_secs() == pytest.approx(0.25)

    def test_zero_for_synchronous_merge(self, monkeypatch):
        # Tests rely on being able to opt in to synchronous merges
        # by setting the env to 0 — pin the contract.
        monkeypatch.setenv(env.ENV_REPORT_DEBOUNCE_SECS, "0")
        assert env.report_debounce_secs() == 0

    def test_negative_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(env.ENV_REPORT_DEBOUNCE_SECS, "-1")
        assert env.report_debounce_secs() == pytest.approx(0.25)


class TestRootIdentity:
    def test_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv(env.ENV_ROOT_INVOKE_ID, raising=False)
        monkeypatch.delenv(env.ENV_ROOT_LOG_DIR, raising=False)
        assert env.root_identity() is None

    def test_partial_returns_none(self, monkeypatch):
        # Only one of the pair set → can't trust it as a live identity.
        monkeypatch.setenv(env.ENV_ROOT_INVOKE_ID, "abc")
        monkeypatch.delenv(env.ENV_ROOT_LOG_DIR, raising=False)
        assert env.root_identity() is None

    def test_both_set_returns_tuple(self, monkeypatch):
        monkeypatch.setenv(env.ENV_ROOT_INVOKE_ID, "abc")
        monkeypatch.setenv(env.ENV_ROOT_LOG_DIR, "/tmp/x")
        assert env.root_identity() == ("abc", "/tmp/x")


class TestFinalizeWaitSeconds:
    def test_default(self, monkeypatch):
        monkeypatch.delenv(env.ENV_FINALIZE_WAIT_SECS, raising=False)
        assert env.read_finalize_wait_seconds() == pytest.approx(120.0)

    def test_explicit_value(self, monkeypatch):
        monkeypatch.setenv(env.ENV_FINALIZE_WAIT_SECS, "5.0")
        assert env.read_finalize_wait_seconds() == pytest.approx(5.0)

    def test_zero_preserves_legacy_seal_immediately(self, monkeypatch):
        # PRD: setting CALLSTACK_FINALIZE_WAIT_SECONDS=0 must be the
        # explicit opt-in to the pre-fix "seal immediately" behavior
        # so tests can pin the legacy report shape if needed.
        monkeypatch.setenv(env.ENV_FINALIZE_WAIT_SECS, "0")
        assert env.read_finalize_wait_seconds() == 0

    def test_huge_value_clamped(self, monkeypatch):
        # Don't let a stray env var hang finalize for an hour.
        monkeypatch.setenv(env.ENV_FINALIZE_WAIT_SECS, "100000")
        assert env.read_finalize_wait_seconds() == 600.0

    @pytest.mark.parametrize("bad", ["-1", "abc", ""])
    def test_invalid_returns_default(self, bad, monkeypatch):
        monkeypatch.setenv(env.ENV_FINALIZE_WAIT_SECS, bad)
        assert env.read_finalize_wait_seconds() == pytest.approx(120.0)


class TestOrphanTtlSeconds:
    def test_default(self, monkeypatch):
        monkeypatch.delenv(env.ENV_ORPHAN_TTL_SECS, raising=False)
        assert env.read_orphan_ttl_seconds() == pytest.approx(1200.0)

    def test_explicit_value(self, monkeypatch):
        monkeypatch.setenv(env.ENV_ORPHAN_TTL_SECS, "300")
        assert env.read_orphan_ttl_seconds() == pytest.approx(300.0)

    def test_zero_opts_out(self, monkeypatch):
        # Documented: 0 = "restore pre-fix behavior" (PID liveness alone).
        monkeypatch.setenv(env.ENV_ORPHAN_TTL_SECS, "0")
        assert env.read_orphan_ttl_seconds() == 0

    def test_huge_value_clamped(self, monkeypatch):
        monkeypatch.setenv(env.ENV_ORPHAN_TTL_SECS, "999999999")
        assert env.read_orphan_ttl_seconds() == 24 * 60 * 60.0

    @pytest.mark.parametrize("bad", ["-1", "abc"])
    def test_invalid_returns_default(self, bad, monkeypatch):
        monkeypatch.setenv(env.ENV_ORPHAN_TTL_SECS, bad)
        assert env.read_orphan_ttl_seconds() == pytest.approx(1200.0)


def test_env_constants_match_string_literals():
    """Lock in the variable names — a rename would break every shell
    integration that exports these. If you intentionally rename one,
    update this test deliberately."""
    assert env.ENV_DEPTH == "CALLSTACK_DEPTH"
    assert env.ENV_ROOT_INVOKE_ID == "CALLSTACK_ROOT_INVOKE_ID"
    assert env.ENV_ROOT_LOG_DIR == "CALLSTACK_ROOT_LOG_DIR"
    assert env.ENV_FRAME_KEY == "CALLSTACK_FRAME_KEY"
    assert env.ENV_OWN_SESSION == "CALLSTACK_OWN_SESSION"
    assert env.ENV_CLAUDE_SESSION == "CLAUDE_CODE_SESSION_ID"
    assert env.ENV_MAX_DEPTH == "CALLSTACK_MAX_DEPTH"
    assert env.ENV_MAX_FANOUT == "CALLSTACK_MAX_FANOUT"
    assert env.ENV_MAX_CONCURRENT_FORKS == "CALLSTACK_MAX_CONCURRENT_FORKS"
    assert env.ENV_MAX_IN_FLIGHT_TURNS == "CALLSTACK_MAX_IN_FLIGHT_TURNS"
    assert env.ENV_REPORT_DEBOUNCE_SECS == "CALLSTACK_REPORT_DEBOUNCE_SECS"
    assert env.ENV_FINALIZE_WAIT_SECS == "CALLSTACK_FINALIZE_WAIT_SECONDS"
    assert env.ENV_ORPHAN_TTL_SECS == "CALLSTACK_ORPHAN_TTL_SECONDS"
