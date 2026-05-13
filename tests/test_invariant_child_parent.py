"""Regression tests for the foundational /call invariant:

    The session a /call forks from is ALWAYS the immediate caller's session —
    not the root's, not an ancestor's, not a sibling's.

History: a regression in the env-var propagation/resolution path made nested
/calls fork from the root instead of the immediate parent. No test had pinned
this invariant down. These tests close that gap at every layer it can break:

    Layer 3 — Caller._driver_for env dict for spawned children.
    Layer 5 — end-to-end recursive scenario (simulated nested child claude).
    Layer 6 — concurrent siblings resolve consistently from the same parent.

Layers 1, 2 live in test_session.py; Layer 4 in test_driver.py.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import agent_callstack as ac
from agent_callstack import Caller
from agent_callstack.channel import ScriptedChannel
from agent_callstack.invocation_ctx import _InvocationContext
from agent_callstack.session import (
    SessionLocator as _RealLocator,
    SessionRef,
    encode_project_dir,
)


# ---------- shared fixtures ----------

ROOT_SID = "00000000-0000-0000-0000-000000000a01"
CHILD_SID = "00000000-0000-0000-0000-000000000a02"
GRANDCHILD_SID = "00000000-0000-0000-0000-000000000a03"


@pytest.fixture
def two_session_world(tmp_path, monkeypatch):
    """A tmp PROJECTS_DIR containing both a 'root' and a 'child' session
    inside the same project folder. Simulates the on-disk state Claude
    Code would have after the root spawned the child.

    Sets the test process cwd to the project folder so locator's cwd
    resolution finds the right project dir.

    Returns (projects_dir, cwd, root_ref, child_ref)."""
    projects = tmp_path / "projects"
    cwd = tmp_path / "proj"
    cwd.mkdir()
    proj_dir = projects / encode_project_dir(str(cwd))
    proj_dir.mkdir(parents=True)

    def _mk(sid: str) -> Path:
        f = proj_dir / f"{sid}.jsonl"
        f.write_text(json.dumps({"cwd": str(cwd), "type": "user"}) + "\n")
        return f

    root_file = _mk(ROOT_SID)
    child_file = _mk(CHILD_SID)

    monkeypatch.chdir(cwd)

    # Redirect SessionLocator's default projects dir for both Caller's
    # locator construction and Driver's resolve_session.
    class _TestLocator(_RealLocator):
        def __init__(self, projects_dir=projects):
            super().__init__(projects_dir)

    monkeypatch.setattr(ac, "SessionLocator", _TestLocator)

    root_ref = SessionRef(session_id=ROOT_SID, file=root_file)
    child_ref = SessionRef(session_id=CHILD_SID, file=child_file)
    return projects, str(cwd), root_ref, child_ref


@pytest.fixture
def capturing_channel(monkeypatch):
    """Replace ClaudeChannel with a fake that captures env and the parent
    session id passed to run_turn. Returns the capture dict so tests can
    inspect it."""
    captured = {
        "envs": [],
        "parent_session_ids": [],
        "instances": [],
    }

    class _CapturingChannel:
        def __init__(self, *, model=None, permission_mode="default",
                     permission_handler=None, env=None):
            self._env = dict(env or {})
            captured["envs"].append(self._env)
            captured["instances"].append(self)
            envelope = ('```json\n'
                        + json.dumps({"op": "return", "result": "ok"})
                        + '\n```')
            self._inner = ScriptedChannel()
            # Pre-load several responses for any sibling fan-out scenarios.
            for _ in range(64):
                self._inner.respond(envelope, GRANDCHILD_SID)

        def run_turn(self, source_session_id, prompt, **kw):
            captured["parent_session_ids"].append(source_session_id)
            return self._inner.run_turn(source_session_id, prompt, **kw)

    monkeypatch.setattr(ac, "ClaudeChannel", _CapturingChannel)
    return captured


# ---------- Layer 3 — Caller._driver_for env dict ----------

class TestDriverForEnv:
    """The spawned-child env dict must not carry the parent's session path.
    Letting it inherit was the root cause of the regression — children would
    see their grandparent's path in CALLSTACK_PARENT_SESSION."""

    def test_spawned_env_omits_stale_parent_session(self, tmp_path,
                                                     capturing_channel):
        parent = SessionRef(
            session_id="00000000-0000-0000-0000-0000000000aa",
            file=tmp_path / "parent.jsonl",
        )
        ctx = _InvocationContext(
            invoke_id="iv-1",
            log_dir=tmp_path / "log",
            cwd=str(tmp_path),
            frame_key="root",
            is_nested=False,
        )
        Caller()._driver_for(parent, ctx=ctx)

        assert capturing_channel["envs"], "ClaudeChannel was not constructed"
        env = capturing_channel["envs"][0]
        assert "CALLSTACK_PARENT_SESSION" not in env, (
            "spawned child must NOT receive CALLSTACK_PARENT_SESSION — "
            "inheritance of that var is what caused grandchildren to fork "
            "from the root instead of their immediate parent"
        )
        # Other propagation keys are unaffected.
        assert env["CALLSTACK_ROOT_INVOKE_ID"] == "iv-1"
        assert env["CALLSTACK_DEPTH"] == "1"


# ---------- Layer 5 — end-to-end recursive scenario ----------

class TestNestedInvariant:
    """Simulates a child claude doing its own /call. The child's env contains
    a stale CALLSTACK_PARENT_SESSION pointing to root (inherited at spawn)
    AND a fresh CLAUDE_SESSION_ID identifying the child process. The locator
    must use the child's identity; otherwise the grandchild forks from root."""

    def test_grandchild_forks_from_child_not_root(self, two_session_world,
                                                    capturing_channel,
                                                    monkeypatch):
        projects, cwd, root, child = two_session_world

        # Child claude's environment after the root spawned it:
        monkeypatch.setenv("CALLSTACK_PARENT_SESSION", str(root.file))
        monkeypatch.setenv("CALLSTACK_ROOT_INVOKE_ID", "root-iv")
        monkeypatch.setenv("CALLSTACK_ROOT_LOG_DIR", cwd + "/.claude/log")
        monkeypatch.setenv("CLAUDE_SESSION_ID", CHILD_SID)
        monkeypatch.delenv("CALLSTACK_DEPTH", raising=False)

        Caller()._invoke(["grandchild task"], context="fork")

        assert capturing_channel["parent_session_ids"], "channel never ran"
        parent_seen = capturing_channel["parent_session_ids"][0]
        assert parent_seen == CHILD_SID, (
            f"grandchild forked from {parent_seen}; expected immediate "
            f"parent {CHILD_SID}. Inherited CALLSTACK_PARENT_SESSION pointed "
            f"at root {ROOT_SID} — the locator picked the stale value "
            f"instead of the per-process CLAUDE_SESSION_ID."
        )
        # Fix A: stale value must not be re-propagated to the grandchild's
        # spawned env either.
        env = capturing_channel["envs"][0]
        assert "CALLSTACK_PARENT_SESSION" not in env


# ---------- Layer 6 — concurrent siblings ----------

class TestConcurrentSiblings:
    """N concurrent /call invocations in the same process all resolve the
    same correct parent — siblings share a parent by definition. Guards
    against any future race in env resolution / per-Caller state."""

    def test_concurrent_siblings_share_parent(self, two_session_world,
                                                capturing_channel,
                                                monkeypatch):
        projects, cwd, root, child = two_session_world

        # Same recursive-child scenario as Layer 5.
        monkeypatch.setenv("CALLSTACK_PARENT_SESSION", str(root.file))
        monkeypatch.setenv("CALLSTACK_ROOT_INVOKE_ID", "root-iv")
        monkeypatch.setenv("CALLSTACK_ROOT_LOG_DIR", cwd + "/.claude/log")
        monkeypatch.setenv("CLAUDE_SESSION_ID", CHILD_SID)
        monkeypatch.delenv("CALLSTACK_DEPTH", raising=False)

        # Each thread issues its own /call; build a fresh Caller per thread
        # to avoid sharing any per-instance state.
        def one(_):
            Caller()._invoke(["sibling"], context="fork")

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(one, range(16)))

        parents = set(capturing_channel["parent_session_ids"])
        assert parents == {CHILD_SID}, (
            f"concurrent siblings forked from inconsistent parents: "
            f"{parents}; expected only {CHILD_SID}"
        )
