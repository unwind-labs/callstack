"""Tests for SessionLocator: discovery + resolution against Claude's project layout."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_callstack.session import SessionLocator, count_lines


@pytest.fixture
def projects(tmp_path) -> Path:
    p = tmp_path / "projects"
    p.mkdir()
    return p


def _make_session(project_dir: Path, name: str, *, cwd: str = "/tmp") -> Path:
    project_dir.mkdir(parents=True, exist_ok=True)
    f = project_dir / f"{name}.jsonl"
    f.write_text(json.dumps({"cwd": cwd, "type": "user"}) + "\n")
    return f


class TestResolve:

    def test_resolve_finds_in_any_project_dir(self, projects):
        f = _make_session(projects / "proj-a", "abc123")
        loc = SessionLocator(projects_dir=projects)
        assert loc.resolve("abc123") == f

    def test_resolve_returns_none_when_missing(self, projects):
        loc = SessionLocator(projects_dir=projects)
        assert loc.resolve("nothing") is None

    def test_resolve_prefers_cwd_matching_project(self, tmp_path, projects):
        from agent_callstack.session import encode_project_dir
        cwd = str(tmp_path / "myproj")
        f = _make_session(projects / encode_project_dir(cwd), "shared", cwd=cwd)
        # Also a different project dir with the same uuid
        _make_session(projects / "other", "shared")
        loc = SessionLocator(projects_dir=projects)
        # cwd match wins
        assert loc.resolve("shared", cwd=cwd) == f


class TestLocate:

    def test_explicit_uuid(self, projects):
        f = _make_session(projects / "p", "explicit-id")
        loc = SessionLocator(projects_dir=projects)
        ref = loc.locate(explicit="explicit-id")
        assert ref.session_id == "explicit-id"
        assert ref.file == f

    def test_explicit_file_path(self, projects):
        f = _make_session(projects / "p", "from-path")
        loc = SessionLocator(projects_dir=projects)
        ref = loc.locate(explicit=str(f))
        assert ref.file == f
        assert ref.session_id == "from-path"

    def test_explicit_missing_raises(self, projects):
        loc = SessionLocator(projects_dir=projects)
        with pytest.raises(RuntimeError, match="not found"):
            loc.locate(explicit="ghost")

    def test_env_var_path_used_when_no_explicit(self, projects, monkeypatch):
        f = _make_session(projects / "p", "env-id")
        loc = SessionLocator(projects_dir=projects)
        monkeypatch.setenv("CALLSTACK_PARENT_SESSION", str(f))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        ref = loc.locate()
        assert ref.file == f

    def test_env_var_uuid_used_when_no_explicit(self, projects, monkeypatch):
        _make_session(projects / "p", "uuid-env")
        loc = SessionLocator(projects_dir=projects)
        monkeypatch.delenv("CALLSTACK_PARENT_SESSION", raising=False)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "uuid-env")
        ref = loc.locate()
        assert ref.session_id == "uuid-env"

    def test_mtime_fallback_picks_most_recent(self, projects, monkeypatch):
        old = _make_session(projects / "p", "old")
        new = _make_session(projects / "p", "new")
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        monkeypatch.delenv("CALLSTACK_PARENT_SESSION", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        loc = SessionLocator(projects_dir=projects)
        ref = loc.locate()
        assert ref.session_id == "new"

    def test_no_session_anywhere_raises(self, projects, monkeypatch):
        monkeypatch.delenv("CALLSTACK_PARENT_SESSION", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        loc = SessionLocator(projects_dir=projects)
        with pytest.raises(RuntimeError, match="Could not discover"):
            loc.locate()


class TestSessionRefCwd:

    def test_extracts_cwd_from_first_message(self, tmp_path):
        from agent_callstack.session import SessionRef
        f = tmp_path / "s.jsonl"
        f.write_text(json.dumps({"cwd": str(tmp_path), "type": "user"}) + "\n")
        ref = SessionRef(session_id="s", file=f)
        assert ref.cwd == str(tmp_path)

    def test_returns_none_if_no_cwd(self, tmp_path):
        from agent_callstack.session import SessionRef
        f = tmp_path / "s.jsonl"
        f.write_text(json.dumps({"type": "user"}) + "\n")
        ref = SessionRef(session_id="s", file=f)
        assert ref.cwd is None


class TestCountLines:

    def test_counts(self, tmp_path):
        f = tmp_path / "x.jsonl"
        f.write_text("a\nb\nc\n")
        assert count_lines(f) == 3

    def test_missing_file_returns_zero(self, tmp_path):
        assert count_lines(tmp_path / "ghost") == 0
