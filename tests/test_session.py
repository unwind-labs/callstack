"""Tests for SessionLocator: discovery + resolution against Claude's project layout."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_callstack.session import SessionLocator, count_lines


# Tests use stable UUID-shaped ids — SessionLocator validates session_id
# shape before any filesystem probe (SEC-003), so placeholder strings
# like "abc123" are rejected.
_SID = {
    "abc123":      "00000000-0000-0000-0000-0000000000a1",
    "shared":      "00000000-0000-0000-0000-0000000000a2",
    "explicit-id": "00000000-0000-0000-0000-0000000000a3",
    "from-path":   "00000000-0000-0000-0000-0000000000a4",
    "ghost":       "00000000-0000-0000-0000-0000000000a5",
    "env-id":      "00000000-0000-0000-0000-0000000000a6",
    "uuid-env":    "00000000-0000-0000-0000-0000000000a7",
    "old":         "00000000-0000-0000-0000-0000000000a8",
    "new":         "00000000-0000-0000-0000-0000000000a9",
    "in-cwd":      "00000000-0000-0000-0000-0000000000b0",
    "elsewhere":   "00000000-0000-0000-0000-0000000000b1",
    "inside":      "00000000-0000-0000-0000-0000000000b2",
    "nothing":     "00000000-0000-0000-0000-0000000000b3",
}


@pytest.fixture
def projects(tmp_path) -> Path:
    p = tmp_path / "projects"
    p.mkdir()
    return p


def _sid(name: str) -> str:
    return _SID.get(name, name)


def _make_session(project_dir: Path, name: str, *, cwd: str = "/tmp") -> Path:
    name = _sid(name)
    project_dir.mkdir(parents=True, exist_ok=True)
    f = project_dir / f"{name}.jsonl"
    f.write_text(json.dumps({"cwd": cwd, "type": "user"}) + "\n")
    return f


class TestResolve:

    def test_resolve_finds_in_any_project_dir(self, projects):
        f = _make_session(projects / "proj-a", "abc123")
        loc = SessionLocator(projects_dir=projects)
        assert loc.resolve(_sid("abc123")) == f

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
        assert loc.resolve(_sid("shared"), cwd=cwd) == f


class TestLocate:

    def test_explicit_uuid(self, projects):
        f = _make_session(projects / "p", "explicit-id")
        loc = SessionLocator(projects_dir=projects)
        ref = loc.locate(explicit=_sid("explicit-id"))
        assert ref.session_id == _sid("explicit-id")
        assert ref.file == f

    def test_explicit_file_path(self, projects):
        f = _make_session(projects / "p", "from-path")
        loc = SessionLocator(projects_dir=projects)
        ref = loc.locate(explicit=str(f))
        assert ref.file == f
        assert ref.session_id == _sid("from-path")

    def test_explicit_missing_raises(self, projects):
        loc = SessionLocator(projects_dir=projects)
        with pytest.raises(RuntimeError, match="not found"):
            loc.locate(explicit=_sid("ghost"))

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
        monkeypatch.setenv("CLAUDE_SESSION_ID", _sid("uuid-env"))
        ref = loc.locate()
        assert ref.session_id == _sid("uuid-env")

    def test_mtime_fallback_picks_most_recent(self, projects, monkeypatch):
        from agent_callstack.session import encode_project_dir
        cwd = "/some/proj"
        proj = projects / encode_project_dir(cwd)
        old = _make_session(proj, "old", cwd=cwd)
        new = _make_session(proj, "new", cwd=cwd)
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        monkeypatch.delenv("CALLSTACK_PARENT_SESSION", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        loc = SessionLocator(projects_dir=projects)
        ref = loc.locate(cwd=cwd)
        assert ref.session_id == _sid("new")

    def test_mtime_fallback_ignores_other_project_dirs(self, projects, monkeypatch):
        """A newer .jsonl in an unrelated project dir must NOT be chosen as
        the parent. Cross-project guessing is what produced wrong
        parent_session values in real reports."""
        from agent_callstack.session import encode_project_dir
        cwd = "/proj/here"
        primary = projects / encode_project_dir(cwd)
        old = _make_session(primary, "in-cwd", cwd=cwd)
        new = _make_session(projects / "other-proj", "elsewhere", cwd="/other")
        os.utime(old, (1000, 1000))
        os.utime(new, (9999, 9999))
        monkeypatch.delenv("CALLSTACK_PARENT_SESSION", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        loc = SessionLocator(projects_dir=projects)
        ref = loc.locate(cwd=cwd)
        assert ref.session_id == _sid("in-cwd")

    def test_mtime_fallback_raises_when_primary_empty(self, projects, monkeypatch):
        """If the cwd-matching project dir has no sessions, refuse rather
        than reach into other projects."""
        from agent_callstack.session import encode_project_dir
        cwd = "/empty/proj"
        (projects / encode_project_dir(cwd)).mkdir(parents=True)
        _make_session(projects / "other-proj", "elsewhere", cwd="/other")
        monkeypatch.delenv("CALLSTACK_PARENT_SESSION", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        loc = SessionLocator(projects_dir=projects)
        with pytest.raises(RuntimeError, match="Could not discover"):
            loc.locate(cwd=cwd)

    def test_env_path_outside_projects_dir_is_rejected(self, projects, tmp_path,
                                                        monkeypatch, capsys):
        """SEC-002: CALLSTACK_PARENT_SESSION pointing at a file outside
        PROJECTS_DIR must be rejected and the locator must fall through to
        the next strategy (rather than blindly opening the attacker-chosen
        file)."""
        outside = tmp_path / "rogue.jsonl"
        outside.write_text(json.dumps({"cwd": "/tmp", "type": "user"}) + "\n")
        # Also put a legitimate session in the cwd-matching project dir so
        # the mtime fallback can find SOMETHING — we want to assert the env
        # path was rejected, not that locate() raised for a different reason.
        from agent_callstack.session import encode_project_dir
        cwd = "/real/proj"
        legit = _make_session(projects / encode_project_dir(cwd),
                              "in-cwd", cwd=cwd)
        monkeypatch.setenv("CALLSTACK_PARENT_SESSION", str(outside))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        loc = SessionLocator(projects_dir=projects)
        ref = loc.locate(cwd=cwd)
        assert ref.file == legit, "rogue env path must be ignored"
        err = capsys.readouterr().err
        assert "rejected" in err and "not under" in err

    def test_env_path_inside_projects_dir_is_accepted(self, projects,
                                                       monkeypatch):
        """Positive control for SEC-002: a path that DOES resolve under
        PROJECTS_DIR is accepted normally."""
        f = _make_session(projects / "p", "inside")
        monkeypatch.setenv("CALLSTACK_PARENT_SESSION", str(f))
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        loc = SessionLocator(projects_dir=projects)
        ref = loc.locate()
        assert ref.file == f

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
