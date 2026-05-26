"""Tests for the cwd-resolution and fork-incompat guard helpers in the MCP
server. These are pure functions, so we test them in isolation without
spinning up the FastMCP runtime."""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

# The plugins directory isn't on sys.path by default — make it importable.
_PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "callstack"
if str(_PLUGIN) not in sys.path:
    sys.path.insert(0, str(_PLUGIN))

import mcp_server  # type: ignore  # noqa: E402
from agent_callstack import MultiResult, Result  # type: ignore  # noqa: E402


class TestResolveCwd:
    def test_empty_returns_parent_project_folder(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        resolved, err = mcp_server._resolve_cwd("")
        assert err is None
        assert Path(resolved).resolve() == tmp_path.resolve()

    def test_pwd_substitution(self, tmp_path, monkeypatch):
        sibling = tmp_path / "sibling"
        sibling.mkdir()
        monkeypatch.chdir(tmp_path)
        resolved, err = mcp_server._resolve_cwd("{PWD}/sibling")
        assert err is None
        assert Path(resolved).resolve() == sibling.resolve()

    def test_pwd_parent_traversal(self, tmp_path, monkeypatch):
        parent_proj = tmp_path / "p"
        sibling_proj = tmp_path / "s"
        parent_proj.mkdir()
        sibling_proj.mkdir()
        monkeypatch.chdir(parent_proj)
        resolved, err = mcp_server._resolve_cwd("{PWD}/../s")
        assert err is None
        assert Path(resolved).resolve() == sibling_proj.resolve()

    def test_nonexistent_dir_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _, err = mcp_server._resolve_cwd("{PWD}/does-not-exist")
        assert err is not None
        assert "not an existing directory" in err

    def test_file_not_dir_returns_error(self, tmp_path, monkeypatch):
        f = tmp_path / "afile.txt"
        f.write_text("x")
        monkeypatch.chdir(tmp_path)
        _, err = mcp_server._resolve_cwd("{PWD}/afile.txt")
        assert err is not None

    def test_pwd_root_allowed(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        resolved, err = mcp_server._resolve_cwd("{PWD}")
        assert err is None
        assert Path(resolved).resolve() == tmp_path.resolve()

    def test_etc_rejected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _, err = mcp_server._resolve_cwd("/etc")
        assert err is not None
        assert "sensitive" in err

    def test_ssh_rejected(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        (fake_home / ".ssh").mkdir(parents=True)
        proj = tmp_path / "proj"
        proj.mkdir()
        monkeypatch.chdir(proj)
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))
        _, err = mcp_server._resolve_cwd("~/.ssh")
        assert err is not None
        assert "sensitive" in err

    def test_symlink_to_etc_rejected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        link = tmp_path / "shortcut"
        os.symlink("/etc", link)
        _, err = mcp_server._resolve_cwd("{PWD}/shortcut")
        assert err is not None
        assert "sensitive" in err

    def test_subdir_of_parent_project_allowed(self, tmp_path, monkeypatch):
        sub = tmp_path / "src"
        sub.mkdir()
        monkeypatch.chdir(tmp_path)
        resolved, err = mcp_server._resolve_cwd("{PWD}/src")
        assert err is None
        assert Path(resolved).resolve() == sub.resolve()


class TestSameProject:
    def test_identical_paths(self, tmp_path):
        assert mcp_server._same_project(str(tmp_path), str(tmp_path))

    def test_via_symlink(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        os.symlink(real, link)
        assert mcp_server._same_project(str(real), str(link))

    def test_different_dirs(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir(); b.mkdir()
        assert not mcp_server._same_project(str(a), str(b))


class TestCallToolGuards:
    """End-to-end checks on the public `call` MCP tool's pre-spawn guards
    (invalid context, bad cwd, fork+cross-project) — these all return error
    envelopes WITHOUT actually spawning claude."""

    @pytest.mark.asyncio
    async def test_invalid_context_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        raw = await mcp_server.call(tasks=["x"], context="bogus")
        env = json.loads(raw)
        assert env["results"][0]["status"] == "error"
        assert "invalid context" in env["results"][0]["error"]

    @pytest.mark.asyncio
    async def test_bad_cwd_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        raw = await mcp_server.call(
            tasks=["x"], context="fresh", cwd="{PWD}/nope",
        )
        env = json.loads(raw)
        assert env["results"][0]["status"] == "error"
        assert "not an existing directory" in env["results"][0]["error"]

    @pytest.mark.asyncio
    async def test_fork_plus_cross_project_returns_error(self, tmp_path, monkeypatch):
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.chdir(tmp_path)
        raw = await mcp_server.call(
            tasks=["x"], context="fork", cwd="{PWD}/other",
        )
        env = json.loads(raw)
        assert env["results"][0]["status"] == "error"
        assert "cannot be combined" in env["results"][0]["error"]


class TestTaskValidation:
    """SEC-102: the MCP boundary caps `len(tasks)` and rejects malformed
    inputs before any subprocess gets spawned. These checks return error
    envelopes without ever calling `caller.call_many`."""

    @pytest.mark.asyncio
    async def test_empty_tasks_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        raw = await mcp_server.call(tasks=[])
        env = json.loads(raw)
        assert env["results"][0]["status"] == "error"
        assert "empty" in env["results"][0]["error"]

    @pytest.mark.asyncio
    async def test_oversize_tasks_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CALLSTACK_MAX_FANOUT", "3")
        raw = await mcp_server.call(tasks=["a", "b", "c", "d"])
        env = json.loads(raw)
        assert env["results"][0]["status"] == "error"
        msg = env["results"][0]["error"]
        assert "max fanout is 3" in msg
        assert "CALLSTACK_MAX_FANOUT" in msg

    @pytest.mark.asyncio
    async def test_non_string_task_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        raw = await mcp_server.call(tasks=["ok", 42])  # type: ignore[list-item]
        env = json.loads(raw)
        assert env["results"][0]["status"] == "error"
        assert "must be a string" in env["results"][0]["error"]

    @pytest.mark.asyncio
    async def test_whitespace_only_task_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        raw = await mcp_server.call(tasks=["real task", "   "])
        env = json.loads(raw)
        assert env["results"][0]["status"] == "error"
        assert "empty or whitespace-only" in env["results"][0]["error"]

    def test_max_fanout_default(self, monkeypatch):
        monkeypatch.delenv("CALLSTACK_MAX_FANOUT", raising=False)
        assert mcp_server._max_fanout() == 64

    def test_max_fanout_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("CALLSTACK_MAX_FANOUT", "not-a-number")
        assert mcp_server._max_fanout() == 64
        monkeypatch.setenv("CALLSTACK_MAX_FANOUT", "0")
        assert mcp_server._max_fanout() == 64
        monkeypatch.setenv("CALLSTACK_MAX_FANOUT", "-5")
        assert mcp_server._max_fanout() == 64


class TestDefaultMaxDepthCeiling:
    """SEC-103: `CALLSTACK_MAX_DEPTH` can be widened by env, but the
    runtime ceiling clamps absurd values to keep depth-bombs from OOM'ing
    the host before any other safety net catches them."""

    def test_ceiling_clamps_huge_values(self, monkeypatch):
        from agent_callstack import _default_max_depth
        monkeypatch.setenv("CALLSTACK_MAX_DEPTH", "1000000")
        assert _default_max_depth() == 32

    def test_legitimate_value_passes_through(self, monkeypatch):
        from agent_callstack import _default_max_depth
        monkeypatch.setenv("CALLSTACK_MAX_DEPTH", "20")
        assert _default_max_depth() == 20

    def test_unset_returns_default(self, monkeypatch):
        from agent_callstack import _default_max_depth
        monkeypatch.delenv("CALLSTACK_MAX_DEPTH", raising=False)
        assert _default_max_depth() == 10

    def test_invalid_returns_default(self, monkeypatch):
        from agent_callstack import _default_max_depth
        monkeypatch.setenv("CALLSTACK_MAX_DEPTH", "abc")
        assert _default_max_depth() == 10


def _ok_result(value: str = "ok") -> Result:
    return Result(value=value, summary=None, next=None,
                  duration=0.01, log=None, log_start=0)


class _StubCaller:
    """Drop-in replacement for `agent_callstack.Caller` that bypasses the
    claude subprocess machinery so we can exercise the run_in_background
    code paths without spawning anything. The `gate` (a threading.Event)
    lets a test hold `call_many` open long enough to observe the
    "pending" branch in `await_call`."""

    def __init__(self, *, results: list, gate: threading.Event | None = None):
        self._results = results
        self._gate = gate

    def call_many(self, tasks, *, context: str = "fork") -> MultiResult:
        if self._gate is not None:
            # Block in the worker thread until the test releases us.
            self._gate.wait(timeout=5.0)
        return MultiResult(results=self._results)


@pytest.fixture(autouse=True)
def _clear_background_registry():
    """Tests share the module-level `_background` registry — wipe it both
    sides of every test so a leak in one doesn't cascade."""
    mcp_server._background.clear()
    yield
    mcp_server._background.clear()


class TestFinalizeAtBoundary:
    """REVIEW-203: `_finalize_at_boundary` is the MCP boundary's escape
    hatch for force-terminating non-terminal frames when something went
    wrong upstream. On the happy path it would be a guaranteed no-op
    (the call_many → driver → reporter.finalize chain already produces
    terminal frames) but still costs a glob + fcntl lock + per-frame
    parse on every tool call. Restrict it to the exception path."""

    @pytest.mark.asyncio
    async def test_happy_path_does_not_invoke_finalize_own_frames(
        self, tmp_path, monkeypatch,
    ):
        """Sync `call` returning normally must not call the boundary guard."""
        monkeypatch.chdir(tmp_path)
        calls = []
        monkeypatch.setattr(
            mcp_server, "_finalize_at_boundary",
            lambda *a, **kw: calls.append((a, kw)),
        )
        monkeypatch.setattr(
            mcp_server, "_build_caller",
            lambda *a, **kw: _StubCaller(results=[_ok_result()]),
        )
        env = json.loads(await mcp_server.call(tasks=["x"]))
        assert env["results"][0]["status"] == "complete"
        assert calls == [], (
            "happy path must not invoke the boundary guard — the "
            "upstream finalize chain is already responsible for terminal "
            "state and the guard was measurable I/O for a guaranteed no-op"
        )

    @pytest.mark.asyncio
    async def test_exception_path_invokes_finalize_own_frames(
        self, tmp_path, monkeypatch,
    ):
        """If call_many raises, the boundary guard must fire so the parent
        sees status='abandoned' rather than a stuck spinner."""
        monkeypatch.chdir(tmp_path)
        calls = []
        monkeypatch.setattr(
            mcp_server, "_finalize_at_boundary",
            lambda *a, **kw: calls.append((a, kw)),
        )

        class _ExplodingCaller:
            def call_many(self, *_a, **_kw):
                raise RuntimeError("boom")

        monkeypatch.setattr(mcp_server, "_build_caller",
                            lambda *a, **kw: _ExplodingCaller())
        with pytest.raises(RuntimeError, match="boom"):
            await mcp_server.call(tasks=["x"])
        assert len(calls) == 1, (
            "exception path must invoke the boundary guard exactly once"
        )

    @pytest.mark.asyncio
    async def test_await_call_happy_path_does_not_invoke_finalize(
        self, tmp_path, monkeypatch,
    ):
        """await_call returning a normal envelope must not call the guard."""
        monkeypatch.chdir(tmp_path)
        calls = []
        monkeypatch.setattr(
            mcp_server, "_finalize_at_boundary",
            lambda *a, **kw: calls.append((a, kw)),
        )
        monkeypatch.setattr(
            mcp_server, "_build_caller",
            lambda *a, **kw: _StubCaller(results=[_ok_result()]),
        )
        started = json.loads(
            await mcp_server.call(tasks=["x"], run_in_background=True),
        )
        env = json.loads(
            await mcp_server.await_call(started["invoke_id"], timeout=5),
        )
        assert env["results"][0]["status"] == "complete"
        assert calls == []


class TestRunInBackground:
    """`run_in_background=True` returns immediately with a 'started'
    envelope; `await_call` reconciles the eventual result. Together they
    let callers avoid the MCP_TOOL_TIMEOUT for long fan-outs without
    losing the structured results envelope."""

    @pytest.mark.asyncio
    async def test_returns_started_envelope_immediately(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        gate = threading.Event()
        monkeypatch.setattr(
            mcp_server, "_build_caller",
            lambda *a, **kw: _StubCaller(results=[_ok_result()], gate=gate),
        )
        raw = await mcp_server.call(tasks=["x"], run_in_background=True)
        env = json.loads(raw)
        assert env["status"] == "started"
        assert env["invoke_id"]
        assert env["report_path"]
        # Task is parked in the registry waiting for the gate.
        assert env["invoke_id"] in mcp_server._background
        # Release and drain so pytest doesn't leave a thread alive past
        # this test. (autouse fixture clears the registry; the task itself
        # needs to be unblocked.)
        gate.set()
        await mcp_server.await_call(env["invoke_id"], timeout=5)

    @pytest.mark.asyncio
    async def test_await_call_returns_full_envelope_when_done(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            mcp_server, "_build_caller",
            lambda *a, **kw: _StubCaller(results=[_ok_result("done")]),
        )
        started = json.loads(
            await mcp_server.call(tasks=["x"], run_in_background=True),
        )
        invoke_id = started["invoke_id"]
        env = json.loads(await mcp_server.await_call(invoke_id, timeout=5))
        # Final envelope matches the sync `call` shape: results list + ids.
        assert env["invoke_id"] == invoke_id
        assert env["report_path"] == started["report_path"]
        assert env["results"][0]["status"] == "complete"
        assert env["results"][0]["result"] == "done"
        # Reconciled tasks are popped so memory doesn't grow unbounded.
        assert invoke_id not in mcp_server._background

    @pytest.mark.asyncio
    async def test_await_call_timeout_returns_pending(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        gate = threading.Event()
        monkeypatch.setattr(
            mcp_server, "_build_caller",
            lambda *a, **kw: _StubCaller(results=[_ok_result()], gate=gate),
        )
        started = json.loads(
            await mcp_server.call(tasks=["x"], run_in_background=True),
        )
        invoke_id = started["invoke_id"]
        # Stub is still blocked on the gate — wait_for should time out.
        env = json.loads(
            await mcp_server.await_call(invoke_id, timeout=0.1),
        )
        assert env["status"] == "pending"
        assert env["invoke_id"] == invoke_id
        # Pending reconciliations keep the entry so the caller can poll.
        assert invoke_id in mcp_server._background
        # Drain.
        gate.set()
        await mcp_server.await_call(invoke_id, timeout=5)

    @pytest.mark.asyncio
    async def test_await_call_unknown_id_returns_error(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        env = json.loads(
            await mcp_server.await_call("does-not-exist", timeout=1),
        )
        assert env["status"] == "error"
        assert "no background call" in env["error"]
        assert env["invoke_id"] == "does-not-exist"

    @pytest.mark.asyncio
    async def test_validation_errors_still_surface_synchronously(
        self, tmp_path, monkeypatch,
    ):
        """Bad input in background mode must NOT silently park a doomed
        task — the orchestrator needs to react immediately, the same way
        it would for a synchronous call."""
        monkeypatch.chdir(tmp_path)
        raw = await mcp_server.call(
            tasks=[], run_in_background=True,
        )
        env = json.loads(raw)
        assert env["results"][0]["status"] == "error"
        # Nothing should have been parked.
        assert len(mcp_server._background) == 0

    @pytest.mark.asyncio
    async def test_background_exception_surfaced_via_await(
        self, tmp_path, monkeypatch,
    ):
        """If `call_many` raises (i.e. an unexpected internal error, not a
        per-task CallFailed), `await_call` must surface it instead of
        hanging or losing it. CallFailed-per-task is handled by the normal
        MultiResult envelope; this test covers the top-level crash path."""
        monkeypatch.chdir(tmp_path)

        class BoomCaller:
            def call_many(self, tasks, *, context="fork"):
                raise RuntimeError("simulated internal failure")

        monkeypatch.setattr(
            mcp_server, "_build_caller", lambda *a, **kw: BoomCaller(),
        )
        started = json.loads(
            await mcp_server.call(tasks=["x"], run_in_background=True),
        )
        invoke_id = started["invoke_id"]
        env = json.loads(await mcp_server.await_call(invoke_id, timeout=5))
        assert env["status"] == "error"
        assert "simulated internal failure" in env["error"]
        # Errored reconciliations also drop the entry — orchestrator has
        # the final word; a second await would only return a stale error.
        assert invoke_id not in mcp_server._background


class TestBackgroundRegistryCap:
    """A leaking orchestrator that fires `run_in_background` calls and
    never reconciles them would grow the registry without bound. The cap
    fails loud instead of silently evicting, so the leak gets noticed."""

    @pytest.mark.asyncio
    async def test_cap_rejects_when_full(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CALLSTACK_MAX_BACKGROUND", "2")
        gate = threading.Event()
        monkeypatch.setattr(
            mcp_server, "_build_caller",
            lambda *a, **kw: _StubCaller(results=[_ok_result()], gate=gate),
        )
        # Park two in the registry.
        e1 = json.loads(
            await mcp_server.call(tasks=["x"], run_in_background=True),
        )
        e2 = json.loads(
            await mcp_server.call(tasks=["x"], run_in_background=True),
        )
        assert e1["status"] == "started" and e2["status"] == "started"
        # Third must be rejected with a clear error pointing at the env knob.
        e3 = json.loads(
            await mcp_server.call(tasks=["x"], run_in_background=True),
        )
        assert e3["results"][0]["status"] == "error"
        msg = e3["results"][0]["error"]
        assert "cap=2" in msg
        assert "CALLSTACK_MAX_BACKGROUND" in msg
        # Drain.
        gate.set()
        await mcp_server.await_call(e1["invoke_id"], timeout=5)
        await mcp_server.await_call(e2["invoke_id"], timeout=5)

    @pytest.mark.asyncio
    async def test_finished_unawaited_tasks_are_reaped(
        self, tmp_path, monkeypatch,
    ):
        """A short-lived background call the orchestrator never `await`ed
        should not occupy a registry slot once it finishes — otherwise an
        agent that fires lots of fast background calls would trip the cap
        even though nothing is actually outstanding."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CALLSTACK_MAX_BACKGROUND", "1")
        # First stub returns instantly (no gate).
        monkeypatch.setattr(
            mcp_server, "_build_caller",
            lambda *a, **kw: _StubCaller(results=[_ok_result()]),
        )
        e1 = json.loads(
            await mcp_server.call(tasks=["x"], run_in_background=True),
        )
        assert e1["status"] == "started"
        # Wait for the underlying task to actually finish so the reaper
        # has something to reap. Using the task handle directly rather
        # than await_call lets us observe the pre-reap state too.
        task = mcp_server._background.task_for(e1["invoke_id"])
        assert task is not None
        await task
        # Slot is still nominally occupied — the reaper runs on the next
        # background call.
        assert e1["invoke_id"] in mcp_server._background
        # Second background call would otherwise hit cap=1, but the
        # reaper drops the done-but-unawaited entry first.
        e2 = json.loads(
            await mcp_server.call(tasks=["x"], run_in_background=True),
        )
        assert e2["status"] == "started"
        assert e1["invoke_id"] not in mcp_server._background
        # Drain.
        await mcp_server.await_call(e2["invoke_id"], timeout=5)
