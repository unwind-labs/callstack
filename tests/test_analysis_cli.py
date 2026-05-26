"""Smoke tests for the four analysis CLI wrapper scripts.

The scripts under `plugins/callstack/resources/analysis/` have rich logic in
`agent_callstack.analysis` (tested in test_analysis.py), but the wrappers
themselves — the sys.path bootstrap, the imports, and the argparse setup —
had no coverage. A stray import error or a botched argument definition in
any wrapper would otherwise ship undetected. Running each with `--help`
exercises import + argparse end-to-end without needing a real trace file.
"""
from __future__ import annotations

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


@pytest.mark.parametrize("script", ["trace_tree.py", "timing_breakdown.py",
                                    "full_report.py", "session_inspect.py"])
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
