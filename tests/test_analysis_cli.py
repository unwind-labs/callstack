"""Tests for the four analysis CLI wrapper scripts.

The scripts under `plugins/callstack/resources/analysis/` are thin formatters
over `agent_callstack.analysis` (tested in test_analysis.py). Two layers of
coverage here:

1. Subprocess smoke tests (`--help`, no-args) exercise the real `__main__`
   entry, the sys.path bootstrap, and the argparse build end-to-end — a stray
   import error or a botched argument definition surfaces as a non-zero exit.
2. In-process tests import each wrapper and call `main()` with a real argv so
   coverage.py measures the script bodies: the happy-path formatting, the
   not-found / empty-trace guards, and the --root prefix resolution that the
   subprocess `--help` runs can never reach.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = (
    Path(__file__).resolve().parents[1]
    / "plugins" / "callstack" / "resources" / "analysis"
)
_SCRIPTS = [
    "session_inspect.py",
    "trace_tree.py",
    "timing_breakdown.py",
    "full_report.py",
]


# ---------- subprocess smoke tests (real __main__ entry) ----------

@pytest.mark.parametrize("script", _SCRIPTS)
def test_cli_help_imports_and_parses(script: str) -> None:
    # `--help` forces import of the script + its `agent_callstack.analysis`
    # dependency and a full argparse build, then exits 0. A regression in
    # the path bootstrap or an argparse misconfiguration surfaces as a
    # non-zero exit / traceback here.
    path = _SCRIPTS_DIR / script
    proc = subprocess.run(
        [sys.executable, str(path), "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, (
        f"{script} --help exited {proc.returncode}; stderr:\n{proc.stderr}"
    )
    assert "usage" in proc.stdout.lower()


@pytest.mark.parametrize("script", _SCRIPTS)
def test_cli_missing_required_path_arg(script: str) -> None:
    # Every wrapper now requires its input path positionally (no stale
    # `call_traces/call_trace.jsonl` default that never resolves). Running
    # with no args must fail with argparse's usage error (exit 2), proving
    # the argument is genuinely required rather than silently defaulted.
    path = _SCRIPTS_DIR / script
    proc = subprocess.run(
        [sys.executable, str(path)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 2, (
        f"{script} with no args exited {proc.returncode}, expected 2; "
        f"stderr:\n{proc.stderr}"
    )


# ---------- in-process helpers (so coverage measures the script body) ----------

def _load(script: str):
    """Import a wrapper script by path as a throwaway module.

    The wrappers aren't on an importable package path (they live under
    resources/), so load them directly from file. Executing the module runs
    its sys.path bootstrap + `from agent_callstack.analysis import ...`, which
    is idempotent across loads."""
    path = _SCRIPTS_DIR / script
    spec = importlib.util.spec_from_file_location(f"_cli_{script[:-3]}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(monkeypatch, script: str, *argv: str):
    """Invoke a wrapper's main() with a fake argv, in-process."""
    mod = _load(script)
    monkeypatch.setattr(sys, "argv", [script, *argv])
    mod.main()


def _write_trace(d: Path, *entries: dict) -> Path:
    path = d / "call_trace.jsonl"
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return path


def _write_session(d: Path, sid: str, *messages: dict, parent: str | None = None):
    path = d / f"{sid}.jsonl"
    with open(path, "w") as f:
        if parent:
            f.write(json.dumps({"parentSessionId": parent, "type": "system"}) + "\n")
        for m in messages:
            f.write(json.dumps(m) + "\n")
    return path


@pytest.fixture
def trace_dir(tmp_path) -> Path:
    d = tmp_path / "call_traces"
    d.mkdir()
    return d


# ---------- session_inspect.py ----------

class TestSessionInspect:
    """Wrapper turns one session JSONL into a human-readable stats block."""

    def test_prints_stats_for_real_session(self, trace_dir, monkeypatch, capsys):
        """Happy path must surface message count, duration, and per-type tally
        — the whole point of the tool for an operator inspecting a run."""
        sf = _write_session(
            trace_dir, "s1",
            {"type": "user", "timestamp": "2026-04-16T10:00:00",
             "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "timestamp": "2026-04-16T10:00:02",
             "message": {"role": "assistant", "content": "yo"}},
        )
        _run(monkeypatch, "session_inspect.py", str(sf))
        out = capsys.readouterr().out
        assert "Session: s1.jsonl" in out
        assert "Messages: 2" in out
        assert "By type:" in out
        assert "user" in out and "assistant" in out

    def test_missing_file_exits_with_message(self, trace_dir, monkeypatch):
        """A bad path must fail loudly (non-zero exit + clear message), not
        print an empty/zeroed report that looks like a real session."""
        with pytest.raises(SystemExit) as exc:
            _run(monkeypatch, "session_inspect.py", str(trace_dir / "ghost.jsonl"))
        assert "session file not found" in str(exc.value)


# ---------- trace_tree.py ----------

class TestTraceTree:
    """Wrapper renders the parent→child call tree from a call_trace.jsonl."""

    def _two_node_trace(self, trace_dir) -> Path:
        f = _write_trace(
            trace_dir,
            {"session_id": "rootaaaa", "task": "main", "duration_seconds": 2.0},
            {"session_id": "childbbb", "task": "sub", "duration_seconds": 1.0},
        )
        _write_session(trace_dir, "rootaaaa", {"type": "user"})
        _write_session(trace_dir, "childbbb", parent="rootaaaa")
        return f

    def test_renders_tree_with_explicit_root(self, trace_dir, monkeypatch, capsys):
        """Operator names a root prefix; the tool must render that subtree."""
        f = self._two_node_trace(trace_dir)
        _run(monkeypatch, "trace_tree.py", str(f), "--root", "rootaaaa")
        out = capsys.readouterr().out
        assert "rootaaaa"[:8] in out
        assert "childbbb"[:8] in out

    def test_renders_tree_with_auto_root(self, trace_dir, monkeypatch, capsys):
        """No --root: the tool must still pick a root and render, not error —
        the common case is "just show me the tree"."""
        f = self._two_node_trace(trace_dir)
        _run(monkeypatch, "trace_tree.py", str(f))
        out = capsys.readouterr().out
        assert "main" in out

    def test_missing_file_exits(self, trace_dir, monkeypatch):
        """Bad trace path is a loud failure, not a blank tree."""
        with pytest.raises(SystemExit) as exc:
            _run(monkeypatch, "trace_tree.py", str(trace_dir / "ghost.jsonl"))
        assert "trace file not found" in str(exc.value)

    def test_empty_trace_exits(self, trace_dir, monkeypatch):
        """An empty trace yields no tree; the tool must say so rather than
        printing nothing and exiting 0 (which reads as success)."""
        f = trace_dir / "call_trace.jsonl"
        f.write_text("")
        with pytest.raises(SystemExit) as exc:
            _run(monkeypatch, "trace_tree.py", str(f))
        assert "nothing to render" in str(exc.value)

    def test_unknown_prefix_exits(self, trace_dir, monkeypatch):
        """A --root prefix matching no session must fail clearly — silently
        falling back to auto-root would render the wrong subtree."""
        f = self._two_node_trace(trace_dir)
        with pytest.raises(SystemExit) as exc:
            _run(monkeypatch, "trace_tree.py", str(f), "--root", "zzz")
        assert "no session id starting with" in str(exc.value)

    def test_ambiguous_prefix_exits(self, trace_dir, monkeypatch):
        """An ambiguous --root prefix must refuse and list candidates rather
        than silently picking one — operator intent is undefined."""
        f = _write_trace(
            trace_dir,
            {"session_id": "abc111", "task": "a", "duration_seconds": 1.0},
            {"session_id": "abc222", "task": "b", "duration_seconds": 1.0},
        )
        _write_session(trace_dir, "abc111", {"type": "user"})
        _write_session(trace_dir, "abc222", {"type": "user"})
        with pytest.raises(SystemExit) as exc:
            _run(monkeypatch, "trace_tree.py", str(f), "--root", "abc")
        assert "ambiguous prefix" in str(exc.value)


# ---------- timing_breakdown.py ----------

class TestTimingBreakdown:
    """Wrapper aggregates duration/turns/errors per session into a table."""

    def test_prints_breakdown_table(self, trace_dir, monkeypatch, capsys):
        """Operator needs the per-session table with a TOTAL row and an error
        tally — that's the entire value of the breakdown."""
        f = _write_trace(
            trace_dir,
            {"session_id": "s1", "duration_seconds": 3.0, "error": None},
            {"session_id": "s1", "duration_seconds": 1.0, "error": "boom"},
            {"session_id": "s2", "duration_seconds": 2.0, "error": None},
        )
        _run(monkeypatch, "timing_breakdown.py", str(f))
        out = capsys.readouterr().out
        assert "session" in out and "duration" in out
        assert "TOTAL" in out
        assert "s1" in out and "s2" in out

    def test_missing_file_exits(self, trace_dir, monkeypatch):
        """Bad path is a loud failure."""
        with pytest.raises(SystemExit) as exc:
            _run(monkeypatch, "timing_breakdown.py", str(trace_dir / "ghost.jsonl"))
        assert "trace file not found" in str(exc.value)

    def test_empty_trace_exits(self, trace_dir, monkeypatch):
        """No events => nothing to break down; must exit with a message, not
        divide-by-zero or print an empty table."""
        f = trace_dir / "call_trace.jsonl"
        f.write_text("")
        with pytest.raises(SystemExit) as exc:
            _run(monkeypatch, "timing_breakdown.py", str(f))
        assert "no events" in str(exc.value)


# ---------- full_report.py ----------

class TestFullReport:
    """Wrapper combines the call tree and the per-session breakdown."""

    def _trace(self, trace_dir) -> Path:
        f = _write_trace(
            trace_dir,
            {"session_id": "rootaaaa", "task": "main", "duration_seconds": 2.0},
            {"session_id": "childbbb", "task": "sub", "duration_seconds": 1.0,
             "error": "boom"},
        )
        _write_session(trace_dir, "rootaaaa", {"type": "user"})
        _write_session(trace_dir, "childbbb", parent="rootaaaa")
        return f

    def test_prints_both_sections(self, trace_dir, monkeypatch, capsys):
        """The report's contract is both halves — tree AND breakdown — in one
        pass; either missing makes it not a "full" report."""
        f = self._trace(trace_dir)
        _run(monkeypatch, "full_report.py", str(f))
        out = capsys.readouterr().out
        assert "CALL TREE" in out
        assert "PER-SESSION BREAKDOWN" in out
        assert "TOTAL" in out

    def test_explicit_root_prefix(self, trace_dir, monkeypatch, capsys):
        """A unique --root prefix selects the tree root for the report."""
        f = self._trace(trace_dir)
        _run(monkeypatch, "full_report.py", str(f), "--root", "root")
        out = capsys.readouterr().out
        assert "CALL TREE" in out

    def test_missing_file_exits(self, trace_dir, monkeypatch):
        """Bad path is a loud failure."""
        with pytest.raises(SystemExit) as exc:
            _run(monkeypatch, "full_report.py", str(trace_dir / "ghost.jsonl"))
        assert "trace file not found" in str(exc.value)

    def test_empty_trace_exits(self, trace_dir, monkeypatch):
        """No events => no report; must exit with a message."""
        f = trace_dir / "call_trace.jsonl"
        f.write_text("")
        with pytest.raises(SystemExit) as exc:
            _run(monkeypatch, "full_report.py", str(f))
        assert "no events in trace" in str(exc.value)

    def test_unknown_prefix_exits(self, trace_dir, monkeypatch):
        """A --root prefix matching nothing must fail, not silently auto-root."""
        f = self._trace(trace_dir)
        with pytest.raises(SystemExit) as exc:
            _run(monkeypatch, "full_report.py", str(f), "--root", "zzz")
        assert "no session id starting with" in str(exc.value)

    def test_ambiguous_prefix_exits(self, trace_dir, monkeypatch):
        """An ambiguous --root prefix must refuse rather than guess."""
        f = _write_trace(
            trace_dir,
            {"session_id": "abc111", "task": "a", "duration_seconds": 1.0},
            {"session_id": "abc222", "task": "b", "duration_seconds": 1.0},
        )
        _write_session(trace_dir, "abc111", {"type": "user"})
        _write_session(trace_dir, "abc222", {"type": "user"})
        with pytest.raises(SystemExit) as exc:
            _run(monkeypatch, "full_report.py", str(f), "--root", "abc")
        assert "ambiguous prefix" in str(exc.value)
