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

    def test_per_process_uuid_wins_over_inherited_path(self, tmp_path, projects,
                                                        monkeypatch):
        """REGRESSION (the core /call invariant): when BOTH env vars are set —
        as happens inside every spawned child claude — the per-process
        CLAUDE_SESSION_ID identifies *this* claude, while CALLSTACK_PARENT_SESSION
        is the value the parent stamped pointing at *its* parent. The locator
        must prefer the per-process identifier; otherwise nested /call grandchildren
        fork from the wrong ancestor (root, not the immediate parent)."""
        from agent_callstack.session import encode_project_dir
        cwd = str(tmp_path / "proj")
        proj = projects / encode_project_dir(cwd)
        # Stale inherited path → root (the grandparent of any further /call).
        root_file = _make_session(proj, "old", cwd=cwd)
        # Fresh per-process identity → child (the immediate parent of /call).
        _make_session(proj, "new", cwd=cwd)

        monkeypatch.setenv("CALLSTACK_PARENT_SESSION", str(root_file))
        monkeypatch.setenv("CLAUDE_SESSION_ID", _sid("new"))
        loc = SessionLocator(projects_dir=projects)
        ref = loc.locate(cwd=cwd)
        assert ref.session_id == _sid("new"), (
            "child claude resolved inherited root path instead of its own "
            "per-process session id — nested /call would fork from root"
        )

    def test_stale_env_path_outside_projects_does_not_mask_uuid(self, tmp_path,
                                                                  projects,
                                                                  monkeypatch):
        """SEC-002 guard composes with the priority order: if the inherited
        path is outside PROJECTS_DIR AND a valid CLAUDE_SESSION_ID is set, the
        UUID resolves; the rogue path is never opened. Both guards together,
        not either alone."""
        from agent_callstack.session import encode_project_dir
        cwd = str(tmp_path / "real")
        proj = projects / encode_project_dir(cwd)
        _make_session(proj, "new", cwd=cwd)
        rogue = tmp_path / "rogue.jsonl"
        rogue.write_text(json.dumps({"cwd": "/elsewhere", "type": "user"}) + "\n")

        monkeypatch.setenv("CALLSTACK_PARENT_SESSION", str(rogue))
        monkeypatch.setenv("CLAUDE_SESSION_ID", _sid("new"))
        loc = SessionLocator(projects_dir=projects)
        ref = loc.locate(cwd=cwd)
        assert ref.session_id == _sid("new")

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


class TestLocateConcurrency:
    """Layer 2 of the /call invariant suite: concurrent locate() must not
    rely on (or mutate) shared global state. Guards against any future
    introduction of module-level caches keyed across callers."""

    def test_concurrent_explicit_resolution(self, projects, monkeypatch):
        from concurrent.futures import ThreadPoolExecutor
        from agent_callstack.session import encode_project_dir

        # Build N distinct sessions in N distinct project dirs.
        n = 16
        sids = [f"00000000-0000-0000-0000-{i:012x}" for i in range(n)]
        for i, sid in enumerate(sids):
            cwd = f"/proj/{i}"
            (projects / encode_project_dir(cwd)).mkdir(parents=True, exist_ok=True)
            f = projects / encode_project_dir(cwd) / f"{sid}.jsonl"
            f.write_text(json.dumps({"cwd": cwd, "type": "user"}) + "\n")

        monkeypatch.delenv("CALLSTACK_PARENT_SESSION", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        loc = SessionLocator(projects_dir=projects)

        def resolve(i):
            sid = sids[i]
            cwd = f"/proj/{i}"
            return loc.locate(explicit=sid, cwd=cwd).session_id

        order = list(range(n)) * 4  # 64 lookups, interleaved
        with ThreadPoolExecutor(max_workers=8) as ex:
            got = list(ex.map(resolve, order))
        assert got == [sids[i] for i in order], (
            "concurrent locate() returned wrong sessions — shared state leak"
        )


class TestCountLines:

    def test_counts(self, tmp_path):
        f = tmp_path / "x.jsonl"
        f.write_text("a\nb\nc\n")
        assert count_lines(f) == 3

    def test_missing_file_returns_zero(self, tmp_path):
        assert count_lines(tmp_path / "ghost") == 0
