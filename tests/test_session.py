"""Tests for session discovery, trace writing, and tree persistence."""

import json
import os
import time

import pytest

from callstack import (
    ExecutionTree,
    TreeNode,
    discover_session,
    resolve_session_file,
    find_active_session_by_mtime,
    write_trace,
    _save_tree,
    _load_tree,
    _extract_cwd_from_session,
    get_cwd_project_dir,
    PROJECTS_DIR,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_projects_dir(tmp_path, monkeypatch):
    """Redirect PROJECTS_DIR and CLAUDE_DIR to a temp directory."""
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr("callstack.PROJECTS_DIR", projects)
    monkeypatch.setattr("callstack.CLAUDE_DIR", tmp_path)
    return projects


def _write_session(directory, session_id, content=None):
    """Create a fake .jsonl session file."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text(content or '{"type":"message"}\n')
    return path


# ---------------------------------------------------------------------------
# _extract_cwd_from_session
# ---------------------------------------------------------------------------

class TestExtractCwdFromSession:

    def test_extracts_cwd(self, tmp_path):
        session = tmp_path / "test.jsonl"
        session.write_text('{"cwd": "/tmp"}\n{"type":"msg"}\n')
        assert _extract_cwd_from_session(session) == "/tmp"

    def test_skips_lines_without_cwd(self, tmp_path):
        session = tmp_path / "test.jsonl"
        session.write_text('{"type":"msg"}\n{"cwd": "/tmp"}\n')
        assert _extract_cwd_from_session(session) == "/tmp"

    def test_returns_none_for_nonexistent_dir(self, tmp_path):
        session = tmp_path / "test.jsonl"
        session.write_text('{"cwd": "/nonexistent/path/xyz123"}\n')
        assert _extract_cwd_from_session(session) is None

    def test_returns_none_for_missing_file(self, tmp_path):
        assert _extract_cwd_from_session(tmp_path / "nope.jsonl") is None

    def test_handles_invalid_json(self, tmp_path):
        session = tmp_path / "test.jsonl"
        session.write_text('not json\n{"cwd": "/tmp"}\n')
        assert _extract_cwd_from_session(session) == "/tmp"


# ---------------------------------------------------------------------------
# resolve_session_file
# ---------------------------------------------------------------------------

class TestResolveSessionFile:

    def test_finds_in_project_dir(self, fake_projects_dir, monkeypatch):
        proj = fake_projects_dir / "-Users-test-project"
        path = _write_session(proj, "abc-123")
        # Monkeypatch get_cwd_project_dir to return our fake project
        monkeypatch.setattr("callstack.get_cwd_project_dir", lambda: proj)
        result = resolve_session_file("abc-123")
        assert result == path

    def test_searches_all_projects(self, fake_projects_dir, monkeypatch):
        proj = fake_projects_dir / "-Users-other-project"
        path = _write_session(proj, "def-456")
        monkeypatch.setattr("callstack.get_cwd_project_dir", lambda: None)
        result = resolve_session_file("def-456")
        assert result == path

    def test_returns_none_when_missing(self, fake_projects_dir, monkeypatch):
        monkeypatch.setattr("callstack.get_cwd_project_dir", lambda: None)
        assert resolve_session_file("nonexistent") is None


# ---------------------------------------------------------------------------
# find_active_session_by_mtime
# ---------------------------------------------------------------------------

class TestFindActiveSessionByMtime:

    def test_returns_most_recent(self, fake_projects_dir):
        proj = fake_projects_dir / "-test"
        proj.mkdir()
        old = _write_session(proj, "old-sess")
        time.sleep(0.05)
        new = _write_session(proj, "new-sess")
        result = find_active_session_by_mtime(proj)
        assert result is not None
        assert result[1] == "new-sess"

    def test_returns_none_when_empty(self, fake_projects_dir):
        proj = fake_projects_dir / "-empty"
        proj.mkdir()
        assert find_active_session_by_mtime(proj) is None


# ---------------------------------------------------------------------------
# discover_session
# ---------------------------------------------------------------------------

class TestDiscoverSession:

    def test_explicit_file_path(self, tmp_path):
        path = _write_session(tmp_path, "explicit")
        session_file, session_id = discover_session(explicit_session_id=str(path))
        assert session_file == path
        assert session_id == "explicit"

    def test_explicit_uuid(self, fake_projects_dir, monkeypatch):
        proj = fake_projects_dir / "-test"
        path = _write_session(proj, "uuid-123")
        session_file, session_id = discover_session(explicit_session_id="uuid-123")
        assert session_file == path
        assert session_id == "uuid-123"

    def test_explicit_uuid_not_found_raises(self, fake_projects_dir):
        with pytest.raises(RuntimeError, match="no matching session file"):
            discover_session(explicit_session_id="nonexistent-uuid")

    def test_env_var_file_path(self, tmp_path, monkeypatch, fake_projects_dir):
        path = _write_session(tmp_path, "env-sess")
        monkeypatch.setenv("CALLSTACK_PARENT_SESSION", str(path))
        # Clear other env vars to avoid interference
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        session_file, session_id = discover_session()
        assert session_file == path
        assert session_id == "env-sess"

    def test_env_var_uuid(self, fake_projects_dir, monkeypatch):
        proj = fake_projects_dir / "-test"
        _write_session(proj, "env-uuid")
        monkeypatch.delenv("CALLSTACK_PARENT_SESSION", raising=False)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "env-uuid")
        session_file, session_id = discover_session()
        assert session_id == "env-uuid"

    def test_mtime_fallback(self, fake_projects_dir, monkeypatch):
        monkeypatch.delenv("CALLSTACK_PARENT_SESSION", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        proj = fake_projects_dir / "-test"
        _write_session(proj, "mtime-sess")
        monkeypatch.setattr("callstack.get_cwd_project_dir", lambda: proj)
        session_file, session_id = discover_session()
        assert session_id == "mtime-sess"

    def test_no_session_raises(self, fake_projects_dir, monkeypatch):
        monkeypatch.delenv("CALLSTACK_PARENT_SESSION", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.setattr("callstack.get_cwd_project_dir", lambda: None)
        with pytest.raises(RuntimeError, match="Could not discover"):
            discover_session()


# ---------------------------------------------------------------------------
# write_trace
# ---------------------------------------------------------------------------

class TestWriteTrace:

    def test_creates_trace_file(self, tmp_path):
        trace_dir = tmp_path / "traces"
        write_trace(trace_dir, 1, "do stuff", "sess-1", "result", 2.5)
        trace_file = trace_dir / "call_trace.jsonl"
        assert trace_file.exists()
        entry = json.loads(trace_file.read_text().strip())
        assert entry["call_depth"] == 1
        assert entry["task"] == "do stuff"
        assert entry["session_id"] == "sess-1"
        assert entry["duration_seconds"] == 2.5
        assert entry["error"] is None

    def test_appends_multiple_entries(self, tmp_path):
        trace_dir = tmp_path / "traces"
        write_trace(trace_dir, 1, "t1", "s1", "r1", 1.0)
        write_trace(trace_dir, 2, "t2", "s2", "r2", 2.0)
        lines = (trace_dir / "call_trace.jsonl").read_text().strip().split("\n")
        assert len(lines) == 2

    def test_records_error(self, tmp_path):
        trace_dir = tmp_path / "traces"
        write_trace(trace_dir, 1, "t", "s", "r", 1.0, error="boom")
        entry = json.loads((trace_dir / "call_trace.jsonl").read_text().strip())
        assert entry["error"] == "boom"

    def test_truncates_long_task(self, tmp_path):
        trace_dir = tmp_path / "traces"
        long_task = "x" * 500
        write_trace(trace_dir, 1, long_task, "s", "r", 1.0)
        entry = json.loads((trace_dir / "call_trace.jsonl").read_text().strip())
        assert len(entry["task"]) == 200


# ---------------------------------------------------------------------------
# _save_tree / _load_tree
# ---------------------------------------------------------------------------

class TestTreePersistence:

    def _make_tree(self):
        return ExecutionTree(
            root_session_id="root",
            root_session_file="/tmp/root.jsonl",
            call_depth_base=1,
            nodes=[TreeNode(id="n1", task="task-1", status="yielded",
                            yield_question="q?", yield_source="n1")],
        )

    def test_save_and_load_round_trip(self, tmp_path):
        clone_path = tmp_path / "clone.jsonl"
        clone_path.write_text("")
        tree = self._make_tree()
        _save_tree(tree, clone_path)

        sidecar = tmp_path / "clone.jsonl.call_tree"
        assert sidecar.exists()

        loaded = _load_tree(clone_path)
        assert loaded is not None
        assert loaded.root_session_id == "root"
        assert len(loaded.nodes) == 1
        assert loaded.nodes[0].yield_question == "q?"

    def test_load_deletes_sidecar(self, tmp_path):
        clone_path = tmp_path / "clone.jsonl"
        clone_path.write_text("")
        _save_tree(self._make_tree(), clone_path)
        sidecar = tmp_path / "clone.jsonl.call_tree"
        assert sidecar.exists()
        _load_tree(clone_path)
        assert not sidecar.exists()

    def test_load_returns_none_when_no_sidecar(self, tmp_path):
        clone_path = tmp_path / "clone.jsonl"
        clone_path.write_text("")
        assert _load_tree(clone_path) is None
