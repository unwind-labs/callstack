"""Boundary tests for `InvocationFactory` — the nested-vs-root identity
decision and child-env propagation lifted out of `Caller` (Task 3).

These exercise the factory directly (no Caller, no Driver), because the
nested-vs-root rule is the single most subtle thing in the package and used
to be reachable only through a full `Caller`. The end-to-end invariants that
this logic must uphold (a grandchild forks from its immediate parent, not the
root) continue to live in `test_invariant_child_parent.py`, which drives the
same factory through the preserved `Caller._driver_for` seam.
"""
from __future__ import annotations

import pytest

from agent_callstack import session
from agent_callstack.env import (
    ENV_CLAUDE_SESSION,
    ENV_DEPTH,
    ENV_FRAME_KEY,
    ENV_MAX_DEPTH,
    ENV_ROOT_INVOKE_ID,
    ENV_ROOT_LOG_DIR,
)
from agent_callstack.invocation import InvocationFactory

# Every CALLSTACK_*/CLAUDE_* var the factory consults. The test process may be
# launched from inside a live callstack invocation (a /call fork), which would
# otherwise leak a "we're nested" signal into the root-path tests.
_CONSULTED_ENV = [
    ENV_ROOT_INVOKE_ID, ENV_ROOT_LOG_DIR, ENV_DEPTH,
    ENV_FRAME_KEY, ENV_CLAUDE_SESSION, ENV_MAX_DEPTH,
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _CONSULTED_ENV:
        monkeypatch.delenv(name, raising=False)


def _factory(**overrides) -> InvocationFactory:
    base = dict(
        explicit_cwd=None,
        explicit_log_dir=None,
        explicit_invoke_id=None,
        max_depth=10,
    )
    base.update(overrides)
    return InvocationFactory(**base)


# ---------- context(): root path ----------

class TestRootContext:
    def test_root_mints_fresh_id_and_is_not_nested(self, tmp_path):
        f = _factory(explicit_cwd=str(tmp_path))
        ctx = f.context(parent_cwd=str(tmp_path))
        assert ctx.is_nested is False
        assert ctx.frame_key == "root"
        assert ctx.instance_id == ""  # root keeps the legacy {frame_key}.yaml name
        assert ctx.invoke_id  # a fresh sortable id was minted
        assert ctx.log_dir == tmp_path / ".claude" / "callstack" / "log"

    def test_root_honors_explicit_invoke_id_and_log_dir(self, tmp_path):
        log = tmp_path / "custom-log"
        f = _factory(explicit_invoke_id="iv-explicit", explicit_log_dir=log,
                     explicit_cwd=str(tmp_path))
        ctx = f.context(parent_cwd=str(tmp_path))
        assert ctx.invoke_id == "iv-explicit"
        assert ctx.log_dir == log
        assert ctx.is_nested is False

    def test_root_uses_parent_cwd_when_no_explicit_cwd(self, tmp_path):
        f = _factory()  # explicit_cwd=None
        ctx = f.context(parent_cwd=str(tmp_path))
        assert ctx.cwd == str(tmp_path)


# ---------- context(): nested path ----------

class TestNestedContext:
    def test_nested_reuses_root_identity_from_env(self, tmp_path, monkeypatch):
        root_log = tmp_path / "root-log"
        monkeypatch.setenv(ENV_ROOT_INVOKE_ID, "root-iv")
        monkeypatch.setenv(ENV_ROOT_LOG_DIR, str(root_log))
        monkeypatch.setenv(ENV_FRAME_KEY, "node-7")

        ctx = _factory(explicit_cwd=str(tmp_path)).context(parent_cwd=str(tmp_path))

        assert ctx.is_nested is True
        assert ctx.invoke_id == "root-iv"     # inherited, NOT minted
        assert ctx.log_dir == root_log
        assert ctx.frame_key == "node-7"
        assert ctx.instance_id != ""          # disambiguates sibling invokes

    def test_explicit_invoke_id_is_ignored_when_nested(self, tmp_path, monkeypatch):
        # A nested call must merge into the root's report regardless of any
        # per-Caller invoke_id — the inherited root identity wins.
        monkeypatch.setenv(ENV_ROOT_INVOKE_ID, "root-iv")
        monkeypatch.setenv(ENV_ROOT_LOG_DIR, str(tmp_path / "rl"))
        ctx = _factory(explicit_invoke_id="should-be-ignored").context(
            parent_cwd=str(tmp_path))
        assert ctx.invoke_id == "root-iv"

    def test_partial_root_env_is_treated_as_root_not_nested(self, tmp_path,
                                                             monkeypatch):
        # Only ROOT_INVOKE_ID set (no ROOT_LOG_DIR) → root_identity() returns
        # None → we must NOT treat this as nested (would write into an
        # undefined log dir).
        monkeypatch.setenv(ENV_ROOT_INVOKE_ID, "root-iv")
        ctx = _factory(explicit_cwd=str(tmp_path)).context(parent_cwd=str(tmp_path))
        assert ctx.is_nested is False

    def test_each_nested_call_gets_a_distinct_instance_id(self, tmp_path,
                                                          monkeypatch):
        monkeypatch.setenv(ENV_ROOT_INVOKE_ID, "root-iv")
        monkeypatch.setenv(ENV_ROOT_LOG_DIR, str(tmp_path / "rl"))
        f = _factory(explicit_cwd=str(tmp_path))
        a = f.context(parent_cwd=str(tmp_path))
        b = f.context(parent_cwd=str(tmp_path))
        assert a.instance_id and b.instance_id
        assert a.instance_id != b.instance_id, (
            "sibling nested invokes from one caller must not share a frame file"
        )


# ---------- frame_key fallback chain ----------

class TestFrameKeyFallback:
    def _nested(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ENV_ROOT_INVOKE_ID, "root-iv")
        monkeypatch.setenv(ENV_ROOT_LOG_DIR, str(tmp_path / "rl"))

    def test_prefers_claude_session_when_no_frame_key(self, tmp_path,
                                                      monkeypatch):
        self._nested(monkeypatch, tmp_path)
        monkeypatch.setenv(ENV_CLAUDE_SESSION, "claude-sid")
        ctx = _factory(explicit_cwd=str(tmp_path)).context(parent_cwd=str(tmp_path))
        assert ctx.frame_key == "claude-sid"

    def test_falls_back_to_pid_when_no_session_discoverable(self, tmp_path,
                                                            monkeypatch):
        self._nested(monkeypatch, tmp_path)
        # No FRAME_KEY, no CLAUDE_CODE_SESSION_ID, and an empty projects dir so
        # session.most_recent_session finds nothing → deterministic pid-* fallback.
        empty_projects = tmp_path / "empty-projects"
        empty_projects.mkdir()
        monkeypatch.setattr(session, "PROJECTS_DIR", empty_projects)
        ctx = _factory(explicit_cwd=str(tmp_path)).context(parent_cwd=str(tmp_path))
        assert ctx.frame_key.startswith("pid-")


# ---------- parent_project_cwd(): cross-project fresh mode ----------

class TestParentProjectCwd:
    def test_nested_prefers_getcwd_over_explicit_child_target(self, tmp_path,
                                                              monkeypatch):
        # In cross-project fresh mode explicit_cwd is the *child's* target dir;
        # the parent session lives in the MCP server's getcwd(). Nested →
        # getcwd() must win so the parent session is located correctly.
        monkeypatch.setenv(ENV_ROOT_INVOKE_ID, "root-iv")
        monkeypatch.setenv(ENV_ROOT_LOG_DIR, str(tmp_path / "rl"))
        monkeypatch.chdir(tmp_path)
        f = _factory(explicit_cwd="/some/redirected/child/target")
        assert f.parent_project_cwd() == str(tmp_path)

    def test_root_uses_explicit_cwd(self, tmp_path):
        f = _factory(explicit_cwd=str(tmp_path / "explicit"))
        assert f.parent_project_cwd() == str(tmp_path / "explicit")

    def test_root_falls_back_to_getcwd_without_explicit_cwd(self, tmp_path,
                                                            monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert _factory().parent_project_cwd() == str(tmp_path)


# ---------- effective_log_dir ----------

class TestEffectiveLogDir:
    def test_default_layout(self, tmp_path):
        f = _factory()
        assert f.effective_log_dir(str(tmp_path)) == (
            tmp_path / ".claude" / "callstack" / "log"
        )

    def test_explicit_overrides_layout(self, tmp_path):
        log = tmp_path / "elsewhere"
        f = _factory(explicit_log_dir=log)
        assert f.effective_log_dir(str(tmp_path)) == log


# ---------- child_env(): propagation to spawned children ----------

class TestChildEnv:
    def _ctx(self, tmp_path):
        return _factory(explicit_cwd=str(tmp_path)).context(parent_cwd=str(tmp_path))

    def test_stamps_depth_root_identity_and_max_depth(self, tmp_path):
        f = _factory(explicit_cwd=str(tmp_path), max_depth=3)
        ctx = f.context(parent_cwd=str(tmp_path))
        env = f.child_env(ctx, depth_base=2)
        assert env[ENV_DEPTH] == "3"                 # depth_base + 1
        assert env[ENV_ROOT_INVOKE_ID] == ctx.invoke_id
        assert env[ENV_ROOT_LOG_DIR] == str(ctx.log_dir)
        assert env[ENV_MAX_DEPTH] == "3"             # CORR-101: explicit cap propagates

    def test_omits_legacy_parent_session_var(self, tmp_path):
        # The regression that motivated this design: inheriting
        # CALLSTACK_PARENT_SESSION made grandchildren fork from the root.
        env = self._factory_env(tmp_path)
        assert "CALLSTACK_PARENT_SESSION" not in env

    def _factory_env(self, tmp_path):
        f = _factory(explicit_cwd=str(tmp_path))
        return f.child_env(f.context(parent_cwd=str(tmp_path)), depth_base=0)

    def test_depth_base_zero_yields_depth_one(self, tmp_path):
        assert self._factory_env(tmp_path)[ENV_DEPTH] == "1"
