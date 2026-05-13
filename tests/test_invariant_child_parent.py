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
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CHILD_SID)
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
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CHILD_SID)
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


# ---------- Layer 7 — env var name (the production-name gotcha) ----------

class TestEnvVarName:
    """Claude Code actually exports `CLAUDE_CODE_SESSION_ID` to each spawned
    claude subprocess (and downstream to its MCP server). The earlier
    locator read `CLAUDE_SESSION_ID` — a var that is never set in production.
    With UUID-first priority, that means locate() silently fell through to
    the mtime heuristic — which under concurrent siblings races and can
    pick the wrong jsonl. Pin the actual env var name down."""

    def test_locator_constant_is_production_env_var_name(self):
        from agent_callstack.session import _ENV_PARENT_UUID
        assert _ENV_PARENT_UUID == "CLAUDE_CODE_SESSION_ID", (
            f"_ENV_PARENT_UUID is {_ENV_PARENT_UUID!r} — Claude Code sets "
            f"CLAUDE_CODE_SESSION_ID per claude subprocess. Reading the wrong "
            f"name causes locate() to fall through to mtime under concurrent "
            f"siblings, which then picks the wrong sibling's session."
        )

    def test_locator_resolves_via_claude_code_session_id(
        self, tmp_path, monkeypatch,
    ):
        from agent_callstack.session import SessionLocator
        cwd = tmp_path / "p"
        cwd.mkdir()
        projects = tmp_path / "projects"
        proj = projects / encode_project_dir(str(cwd))
        proj.mkdir(parents=True)
        sid = "00000000-0000-0000-0000-00000000c0de"
        (proj / f"{sid}.jsonl").write_text(
            json.dumps({"cwd": str(cwd), "type": "user"}) + "\n"
        )
        monkeypatch.delenv("CALLSTACK_PARENT_SESSION", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

        ref = SessionLocator(projects_dir=projects).locate(cwd=str(cwd))
        assert ref.session_id == sid


# ---------- Layer 8 — depth 3+ with parallel siblings at each level ----------

def _setup_session_world(tmp_path, monkeypatch, sids):
    """Build a projects dir containing one .jsonl per sid. Returns
    (projects_dir, cwd, {sid: SessionRef})."""
    projects = tmp_path / "projects"
    cwd = tmp_path / "proj"
    cwd.mkdir()
    proj_dir = projects / encode_project_dir(str(cwd))
    proj_dir.mkdir(parents=True)
    refs = {}
    for sid in sids:
        f = proj_dir / f"{sid}.jsonl"
        f.write_text(json.dumps({"cwd": str(cwd), "type": "user"}) + "\n")
        refs[sid] = SessionRef(session_id=sid, file=f)
    monkeypatch.chdir(cwd)

    class _TestLocator(_RealLocator):
        def __init__(self, projects_dir=projects):
            super().__init__(projects_dir)
    monkeypatch.setattr(ac, "SessionLocator", _TestLocator)
    return projects, str(cwd), refs


class TestDeeperDepthParallelSiblings:
    """The exact scenario the user observed: at depth 2, parallel sibling
    /calls dispatched from one node spawn grandchildren that ended up
    forking from the WRONG sibling's session.

    Mechanism: each child claude's MCP server resolved 'self' via a stale
    env path or an mtime heuristic that races when siblings run
    concurrently. With CLAUDE_CODE_SESSION_ID read correctly, each child
    claude identifies itself unambiguously — regardless of sibling activity
    or stale env leakage."""

    def test_depth_3_parallel_siblings_dont_cross_contaminate(
        self, tmp_path, monkeypatch,
    ):
        """root → {A, B} → each of A and B dispatches its own grandchildren.
        Worst-case env: stale CALLSTACK_PARENT_SESSION points to root (as
        a pre-fix runtime would have propagated), and B's jsonl is mtime-
        newer than A's (so any mtime-based fallback prefers B).

        Invariant: A's grandchildren MUST fork from A, B's from B."""
        ROOT = "00000000-0000-0000-0000-000000003001"
        A    = "00000000-0000-0000-0000-000000003002"  # noqa: E221
        B    = "00000000-0000-0000-0000-000000003003"  # noqa: E221
        projects, cwd, refs = _setup_session_world(
            tmp_path, monkeypatch, [ROOT, A, B],
        )

        # Bump B's mtime so it'd win an mtime heuristic.
        import time as _time
        _time.sleep(0.01)
        refs[B].file.write_text(refs[B].file.read_text() + " ")

        # Per-call capture (each Caller() builds its own ClaudeChannel
        # instance; capture instance-by-instance).
        captures = []

        class _PerInvokeChannel:
            def __init__(self, *, model=None, permission_mode="default",
                         permission_handler=None, env=None):
                self.parent_session_ids = []
                self.env = dict(env or {})
                self._inner = ScriptedChannel()
                env_done = ('```json\n'
                            + json.dumps({"op": "return", "result": "ok"})
                            + '\n```')
                for _ in range(64):
                    self._inner.respond(env_done, GRANDCHILD_SID)
                captures.append(self)

            def run_turn(self, source_session_id, prompt, **kw):
                self.parent_session_ids.append(source_session_id)
                return self._inner.run_turn(source_session_id, prompt, **kw)

        monkeypatch.setattr(ac, "ClaudeChannel", _PerInvokeChannel)

        # Common env every nested child claude sees.
        monkeypatch.setenv("CALLSTACK_ROOT_INVOKE_ID", "root-iv")
        monkeypatch.setenv("CALLSTACK_ROOT_LOG_DIR", cwd + "/.claude/log")
        monkeypatch.setenv("CALLSTACK_PARENT_SESSION", str(refs[ROOT].file))
        monkeypatch.delenv("CALLSTACK_DEPTH", raising=False)

        # ---- Inside A's claude: dispatch A's grandchildren ----
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", A)
        Caller()._invoke(["A-gc-1", "A-gc-2"], context="fork")

        # ---- Inside B's claude: dispatch B's grandchildren ----
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", B)
        Caller()._invoke(["B-gc-1", "B-gc-2"], context="fork")

        assert len(captures) == 2, (
            f"expected one channel per _invoke, got {len(captures)}"
        )
        a_channel, b_channel = captures
        assert set(a_channel.parent_session_ids) == {A}, (
            f"A's grandchildren forked from {a_channel.parent_session_ids}; "
            f"expected all {A} (sibling B's jsonl was mtime-newer — fix must "
            f"prefer per-process UUID over filesystem mtime)"
        )
        assert set(b_channel.parent_session_ids) == {B}, (
            f"B's grandchildren forked from {b_channel.parent_session_ids}; "
            f"expected all {B}"
        )

    def test_depth_4_each_level_correctly_identified(
        self, tmp_path, monkeypatch,
    ):
        """Walk a depth-4 chain with multiple siblings at each level. At
        every simulated level, the locator inside that level's process
        must return that level's session — never an ancestor, never a
        sibling. Stale CALLSTACK_PARENT_SESSION leaks root throughout."""
        from agent_callstack.session import SessionLocator

        levels = {
            "root": "00000000-0000-0000-0000-000000004001",
            "L1a":  "00000000-0000-0000-0000-000000004002",
            "L1b":  "00000000-0000-0000-0000-000000004003",
            "L2a":  "00000000-0000-0000-0000-000000004004",
            "L2b":  "00000000-0000-0000-0000-000000004005",
            "L3a":  "00000000-0000-0000-0000-000000004006",
            "L3b":  "00000000-0000-0000-0000-000000004007",
        }
        projects, cwd, _ = _setup_session_world(
            tmp_path, monkeypatch, list(levels.values()),
        )
        # Worst case: every level still has CALLSTACK_PARENT_SESSION leaking
        # the root.jsonl (would happen if any upstream MCP server was running
        # pre-fix code that still propagates the var).
        root_file = (projects / encode_project_dir(cwd)
                     / f"{levels['root']}.jsonl")
        monkeypatch.setenv("CALLSTACK_PARENT_SESSION", str(root_file))

        for label, sid in levels.items():
            monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
            ref = SessionLocator(projects_dir=projects).locate(cwd=cwd)
            assert ref.session_id == sid, (
                f"At simulated level {label!r}, locator returned "
                f"{ref.session_id} instead of {sid}. With stale "
                f"CALLSTACK_PARENT_SESSION leaking root in env, the "
                f"per-process CLAUDE_CODE_SESSION_ID must always win."
            )
