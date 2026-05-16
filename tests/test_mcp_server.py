"""Tests for the cwd-resolution and fork-incompat guard helpers in the MCP
server. These are pure functions, so we test them in isolation without
spinning up the FastMCP runtime."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# The plugins directory isn't on sys.path by default — make it importable.
_PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "callstack"
if str(_PLUGIN) not in sys.path:
    sys.path.insert(0, str(_PLUGIN))

import mcp_server  # type: ignore  # noqa: E402


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
