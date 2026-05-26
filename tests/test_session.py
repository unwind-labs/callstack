"""Tests for SessionLocator: discovery + resolution against Claude's project layout."""

from __future__ import annotations

import json
import os
from pathlib import Path

import agent_callstack.session as session_mod
import pytest
from agent_callstack.session import (
    _SESSION_INDEX_FILENAME,
    SessionLocator,
    _extract_cwd,
    _load_session_index,
    _save_session_index,
    count_lines,
    encode_project_dir,
    envelope_from_session_record,
    session_record_epoch,
)

# Tests use stable UUID-shaped ids — SessionLocator validates session_id
# shape before any filesystem probe (SEC-003), so placeholder strings
# like "abc123" are rejected.
_SID = {
    "abc123": "00000000-0000-0000-0000-0000000000a1",
    "shared": "00000000-0000-0000-0000-0000000000a2",
    "explicit-id": "00000000-0000-0000-0000-0000000000a3",
    "from-path": "00000000-0000-0000-0000-0000000000a4",
    "ghost": "00000000-0000-0000-0000-0000000000a5",
    "env-id": "00000000-0000-0000-0000-0000000000a6",
    "uuid-env": "00000000-0000-0000-0000-0000000000a7",
    "old": "00000000-0000-0000-0000-0000000000a8",
    "new": "00000000-0000-0000-0000-0000000000a9",
    "in-cwd": "00000000-0000-0000-0000-0000000000b0",
    "elsewhere": "00000000-0000-0000-0000-0000000000b1",
    "inside": "00000000-0000-0000-0000-0000000000b2",
    "nothing": "00000000-0000-0000-0000-0000000000b3",
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

    def test_env_var_uuid_used_when_no_explicit(self, projects, monkeypatch):
        _make_session(projects / "p", "uuid-env")
        loc = SessionLocator(projects_dir=projects)
        monkeypatch.delenv("CALLSTACK_OWN_SESSION", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _sid("uuid-env"))
        ref = loc.locate()
        assert ref.session_id == _sid("uuid-env")

    def test_own_session_wins_over_claude_code_session(self, tmp_path, projects, monkeypatch):
        """REGRESSION (the core /call invariant): when both env vars are
        present, CALLSTACK_OWN_SESSION (stamped by the spawning parent
        alongside `claude --session-id <uuid>`) wins over
        CLAUDE_CODE_SESSION_ID. The latter may have leaked from the
        grandparent's env (Claude Code's MCP-server env-propagation
        behavior across `--fork-session` is opaque), so we cannot rely
        on it inside a spawned child."""
        from agent_callstack.session import encode_project_dir

        cwd = str(tmp_path / "proj")
        proj = projects / encode_project_dir(cwd)
        # Stale CLAUDE_CODE_SESSION_ID inherited from grandparent.
        _make_session(proj, "old", cwd=cwd)
        # Our own session — what we were spawned to be.
        _make_session(proj, "new", cwd=cwd)

        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _sid("old"))
        monkeypatch.setenv("CALLSTACK_OWN_SESSION", _sid("new"))
        loc = SessionLocator(projects_dir=projects)
        ref = loc.locate(cwd=cwd)
        assert ref.session_id == _sid("new"), (
            "spawned child resolved inherited CLAUDE_CODE_SESSION_ID "
            "instead of its own CALLSTACK_OWN_SESSION — nested /call "
            "would fork from the wrong ancestor"
        )

    def test_mtime_fallback_picks_most_recent(self, projects, monkeypatch):
        from agent_callstack.session import encode_project_dir

        cwd = "/some/proj"
        proj = projects / encode_project_dir(cwd)
        old = _make_session(proj, "old", cwd=cwd)
        new = _make_session(proj, "new", cwd=cwd)
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CALLSTACK_OWN_SESSION", raising=False)
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
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CALLSTACK_OWN_SESSION", raising=False)
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
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CALLSTACK_OWN_SESSION", raising=False)
        loc = SessionLocator(projects_dir=projects)
        with pytest.raises(RuntimeError, match="Could not discover"):
            loc.locate(cwd=cwd)

    def test_legacy_parent_session_env_is_ignored(self, projects, tmp_path, monkeypatch, capsys):
        """The legacy CALLSTACK_PARENT_SESSION env was removed (it caused
        cross-fork by inheriting a grandparent's value through arbitrary
        nesting depth). Setting it must be a no-op — the locator must
        not open the file it points at, even if that file is a valid
        session under PROJECTS_DIR."""
        from agent_callstack.session import encode_project_dir

        # Legacy env points at an arbitrary jsonl that DOES exist.
        rogue_proj = projects / "rogue"
        rogue = _make_session(rogue_proj, "in-cwd")
        # Real cwd has its own session — mtime should pick this one.
        cwd = str(tmp_path / "real")
        proj = projects / encode_project_dir(cwd)
        legit = _make_session(proj, "elsewhere", cwd=cwd)
        monkeypatch.setenv("CALLSTACK_PARENT_SESSION", str(rogue))
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CALLSTACK_OWN_SESSION", raising=False)
        monkeypatch.delenv("CALLSTACK_ROOT_INVOKE_ID", raising=False)
        loc = SessionLocator(projects_dir=projects)
        ref = loc.locate(cwd=cwd)
        assert ref.file == legit, "legacy CALLSTACK_PARENT_SESSION must not influence locate()"

    def test_no_session_anywhere_raises(self, projects, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CALLSTACK_OWN_SESSION", raising=False)
        loc = SessionLocator(projects_dir=projects)
        with pytest.raises(RuntimeError, match="Could not discover"):
            loc.locate()

    def test_nested_invocation_refuses_mtime_fallback(self, projects, monkeypatch):
        """The /call cross-fork guard: inside a nested invocation
        (CALLSTACK_ROOT_INVOKE_ID set) with neither session env var present,
        locate() must REFUSE the mtime heuristic and fail loud — under
        concurrent sibling /calls the most-recently-touched .jsonl races, so
        guessing would resolve a sibling as the parent and corrupt the tree."""
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CALLSTACK_OWN_SESSION", raising=False)
        monkeypatch.setenv("CALLSTACK_ROOT_INVOKE_ID", "20260526T000000-deadbeef")
        loc = SessionLocator(projects_dir=projects)
        with pytest.raises(RuntimeError, match="nested invocation"):
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

        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CALLSTACK_OWN_SESSION", raising=False)
        loc = SessionLocator(projects_dir=projects)

        def resolve(i):
            sid = sids[i]
            cwd = f"/proj/{i}"
            return loc.locate(explicit=sid, cwd=cwd).session_id

        order = list(range(n)) * 4  # 64 lookups, interleaved
        with ThreadPoolExecutor(max_workers=8) as ex:
            got = list(ex.map(resolve, order))
        assert got == [sids[i] for i in order], "concurrent locate() returned wrong sessions — shared state leak"


class TestCountLines:
    def test_counts(self, tmp_path):
        f = tmp_path / "x.jsonl"
        f.write_text("a\nb\nc\n")
        assert count_lines(f) == 3

    def test_missing_file_returns_zero(self, tmp_path):
        assert count_lines(tmp_path / "ghost") == 0


class TestMostRecentSession:
    """Boundary tests for the public `most_recent_session` helper — the
    mtime-based 'which session is the caller' lookup that used to live in
    frames.py and is now owned by session.py. Callers reach for it only
    after deterministic env signals are absent, so it answers a single
    question: which `.jsonl` in cwd's project dir was touched last?"""

    def test_returns_none_for_empty_project_dir(self, projects, monkeypatch):
        import agent_callstack.session as session_mod
        from agent_callstack.session import encode_project_dir

        cwd = "/some/proj"
        (projects / encode_project_dir(cwd)).mkdir(parents=True)
        monkeypatch.setattr(session_mod, "PROJECTS_DIR", projects)
        assert session_mod.most_recent_session(cwd) is None

    def test_returns_none_when_no_project_dir(self, projects, monkeypatch):
        import agent_callstack.session as session_mod

        monkeypatch.setattr(session_mod, "PROJECTS_DIR", projects)
        assert session_mod.most_recent_session("/never/created") is None

    def test_picks_newest_by_mtime(self, projects, monkeypatch):
        import agent_callstack.session as session_mod
        from agent_callstack.session import encode_project_dir

        cwd = "/some/proj"
        proj = projects / encode_project_dir(cwd)
        old = _make_session(proj, "old", cwd=cwd)
        new = _make_session(proj, "new", cwd=cwd)
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        monkeypatch.setattr(session_mod, "PROJECTS_DIR", projects)
        assert session_mod.most_recent_session(cwd) == _sid("new")

    def test_recreates_shared_locator_when_projects_dir_swapped(self, tmp_path, monkeypatch):
        """The module-level shared locator binds PROJECTS_DIR at construction.
        Swapping the global (as tests and a reloaded runtime do) must
        transparently recreate it so the new tree is honored — otherwise a
        stale locator would keep scanning the old directory."""
        import agent_callstack.session as session_mod
        from agent_callstack.session import encode_project_dir

        cwd = "/swap/proj"
        first = tmp_path / "first"
        first.mkdir()
        _make_session(first / encode_project_dir(cwd), "old", cwd=cwd)
        monkeypatch.setattr(session_mod, "PROJECTS_DIR", first)
        assert session_mod.most_recent_session(cwd) == _sid("old")
        second = tmp_path / "second"
        second.mkdir()
        _make_session(second / encode_project_dir(cwd), "new", cwd=cwd)
        monkeypatch.setattr(session_mod, "PROJECTS_DIR", second)
        assert session_mod.most_recent_session(cwd) == _sid("new")

    def test_reflects_newer_session_added_after_first_call(self, projects, monkeypatch):
        """The shared locator caches per-cwd results keyed on the project
        dir's mtime; a newer session added later must invalidate that cache.
        Encodes the no-stale-read guarantee, not the optimization itself."""
        import agent_callstack.session as session_mod
        from agent_callstack.session import encode_project_dir

        cwd = "/grow/proj"
        proj = projects / encode_project_dir(cwd)
        old = _make_session(proj, "old", cwd=cwd)
        os.utime(old, (1000, 1000))
        os.utime(proj, (1000, 1000))
        monkeypatch.setattr(session_mod, "PROJECTS_DIR", projects)
        assert session_mod.most_recent_session(cwd) == _sid("old")
        new = _make_session(proj, "new", cwd=cwd)
        os.utime(new, (2000, 2000))
        os.utime(proj, (2000, 2000))  # bump dir mtime → cache invalidates
        assert session_mod.most_recent_session(cwd) == _sid("new")


class TestLocateEnvFallthrough:
    """locate() walks the env-var priority chain; a var that is set but whose
    value doesn't resolve to a real session must be skipped, not treated as the
    answer — otherwise a stale id would shadow a later, valid one."""

    def test_unresolvable_env_var_is_skipped_for_next(self, projects, monkeypatch):
        # CALLSTACK_OWN_SESSION points at a UUID with no file on disk; the
        # locator must fall through to the resolvable CLAUDE_CODE_SESSION_ID.
        _make_session(projects / "p", "uuid-env")
        monkeypatch.setenv("CALLSTACK_OWN_SESSION", _sid("ghost"))  # no file
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _sid("uuid-env"))
        monkeypatch.delenv("CALLSTACK_ROOT_INVOKE_ID", raising=False)
        loc = SessionLocator(projects_dir=projects)
        assert loc.locate().session_id == _sid("uuid-env")


class TestResolveIndexAndScan:
    """resolve() tries cwd-match → persisted index → full scan. The index hit
    and the scan's skip branches (non-dir entries, non-jsonl files, unrelated
    sessions) are the paths the cwd-match happy case never exercises."""

    def test_index_hit_returns_without_scan(self, projects):
        f = _make_session(projects / "proj-a", "abc123")
        # Pre-seed the index so resolve() returns via the index, not a scan.
        _save_session_index(projects, {_sid("abc123"): "proj-a"})
        loc = SessionLocator(projects_dir=projects)
        assert loc.resolve(_sid("abc123")) == f

    def test_scan_skips_nondir_and_nonjsonl_and_unrelated(self, projects):
        # A stray file at the projects root (not a dir) must be skipped.
        (projects / "loose.txt").write_text("x")
        # A project dir containing a non-jsonl file and an unrelated session.
        proj = projects / "proj-a"
        proj.mkdir()
        (proj / "notes.md").write_text("ignore me")
        _make_session(proj, "elsewhere")  # unrelated sid, found stays None
        target = _make_session(proj, "abc123")
        loc = SessionLocator(projects_dir=projects)
        assert loc.resolve(_sid("abc123")) == target
        # The scan populated the index with everything it saw.
        idx = _load_session_index(projects)
        assert idx.get(_sid("abc123")) == "proj-a"
        assert idx.get(_sid("elsewhere")) == "proj-a"

    def test_scan_tolerates_unreadable_project_dir(self, projects, monkeypatch):
        """A project dir that raises on scandir (perm denied, vanished mid-scan)
        must be skipped so one bad dir can't abort discovery of others."""
        proj = projects / "proj-a"
        target = _make_session(proj, "abc123")
        bad = projects / "locked"
        bad.mkdir()
        real_scandir = os.scandir

        def flaky_scandir(path):
            if Path(path) == bad:
                raise OSError("permission denied")
            return real_scandir(path)

        monkeypatch.setattr(session_mod.os, "scandir", flaky_scandir)
        loc = SessionLocator(projects_dir=projects)
        assert loc.resolve(_sid("abc123")) == target


class TestMostRecentBranches:
    """_most_recent caches per-cwd keyed on the project dir mtime and skips
    non-jsonl files. The cache-hit short-circuit and the file-type skip are
    branches the freshness tests above don't pin directly."""

    def test_cache_hit_returns_same_ref_without_rescan(self, projects, monkeypatch):
        cwd = "/cache/proj"
        proj = projects / encode_project_dir(cwd)
        _make_session(proj, "old", cwd=cwd)
        loc = SessionLocator(projects_dir=projects)
        first = loc._most_recent(cwd)
        # Second call with the dir mtime unchanged must return the cached ref
        # without a rescan — make a rescan fail loudly to prove it isn't run.
        monkeypatch.setattr(session_mod.os, "scandir", lambda *_a: pytest.fail("rescanned despite cache hit"))
        assert loc._most_recent(cwd) is first

    def test_skips_non_jsonl_files(self, projects):
        cwd = "/mixed/proj"
        proj = projects / encode_project_dir(cwd)
        sess = _make_session(proj, "new", cwd=cwd)
        (proj / "README.txt").write_text("not a session")
        loc = SessionLocator(projects_dir=projects)
        ref = loc._most_recent(cwd)
        assert ref is not None and ref.file == sess


class TestEnvelopeFromSessionRecord:
    """envelope_from_session_record owns the session-record *shape*: only an
    assistant row whose content list holds a text block with a parseable
    envelope yields one. Every other shape must return None so a quoted or
    malformed envelope is never mistaken for the agent's terminal signal."""

    def _assistant(self, content):
        return {"message": {"role": "assistant", "content": content}}

    def test_returns_envelope_from_text_block(self):
        from agent_callstack.protocol import Return

        rec = self._assistant(
            [
                {"type": "text", "text": '```json\n{"op": "return", "result": "ok"}\n```'},
            ]
        )
        env = envelope_from_session_record(rec)
        assert isinstance(env, Return) and env.result == "ok"

    def test_non_assistant_row_ignored(self):
        assert envelope_from_session_record({"message": {"role": "user", "content": []}}) is None

    def test_content_not_a_list_returns_none(self):
        assert envelope_from_session_record({"message": {"role": "assistant", "content": "oops"}}) is None

    def test_non_text_blocks_and_no_envelope_returns_none(self):
        rec = self._assistant(
            [
                {"type": "tool_use", "name": "Bash"},  # non-text block skipped
                {"type": "text", "text": 123},  # text not a str skipped
                {"type": "text", "text": "just prose, no fence"},  # parses to None
            ]
        )
        assert envelope_from_session_record(rec) is None


class TestSessionRecordEpoch:
    """session_record_epoch converts a record's ISO-8601 timestamp to epoch
    seconds; an absent or unparseable timestamp yields None rather than raising,
    so a malformed row can't crash report assembly."""

    def test_parses_iso_timestamp(self):
        epoch = session_record_epoch({"timestamp": "2026-05-18T15:49:09.206Z"})
        assert epoch is not None and epoch > 0

    def test_missing_timestamp_returns_none(self):
        assert session_record_epoch({}) is None

    def test_unparseable_timestamp_returns_none(self):
        assert session_record_epoch({"timestamp": "not-a-date"}) is None


class TestExtractCwd:
    """_extract_cwd returns the first recorded cwd that still exists on disk,
    skipping blank lines and unparseable rows so a noisy JSONL prefix doesn't
    hide a valid cwd later in the file."""

    def test_skips_blank_lines_then_finds_cwd(self, tmp_path):
        f = tmp_path / "s.jsonl"
        f.write_text("\n   \n" + json.dumps({"cwd": str(tmp_path)}) + "\n")
        assert _extract_cwd(f) == str(tmp_path)

    def test_missing_file_returns_none(self, tmp_path):
        assert _extract_cwd(tmp_path / "ghost.jsonl") is None

    def test_skips_unparseable_line_then_finds_cwd(self, tmp_path):
        f = tmp_path / "s.jsonl"
        f.write_text("{not valid json\n" + json.dumps({"cwd": str(tmp_path)}) + "\n")
        assert _extract_cwd(f) == str(tmp_path)


class TestSessionIndexPersistence:
    """The lazy session index is best-effort: a corrupt or non-dict file loads
    as empty, a round-trip survives, and write failures (bad dir, unserializable
    payload) never silently corrupt the on-disk index."""

    def test_load_missing_returns_empty(self, projects):
        assert _load_session_index(projects) == {}

    def test_roundtrip_filters_non_string_entries(self, projects):
        _save_session_index(projects, {"a": "dir-a"})
        assert _load_session_index(projects) == {"a": "dir-a"}

    def test_non_dict_index_file_loads_as_empty(self, projects):
        (projects / _SESSION_INDEX_FILENAME).write_text(json.dumps(["not", "a", "dict"]))
        assert _load_session_index(projects) == {}

    def test_save_to_missing_dir_is_silent(self, tmp_path):
        # mkstemp on a nonexistent dir raises OSError; save must swallow it.
        _save_session_index(tmp_path / "does-not-exist", {"a": "b"})  # no raise

    def test_save_unserializable_payload_cleans_up_and_raises(self, projects):
        # json.dump fails on a non-serializable value after the temp file is
        # opened; the temp file must be unlinked and the error re-raised (not
        # left as a dangling .tmp).
        with pytest.raises(TypeError):
            _save_session_index(projects, {"a": object()})  # type: ignore[dict-item]
        leftovers = list(projects.glob(_SESSION_INDEX_FILENAME + ".*"))
        assert leftovers == [], f"temp index file left behind: {leftovers}"
