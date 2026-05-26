"""Test-only helpers (ARCH-13: legacy one-shot report writer used by
tests for back-to-back assertions of merged-report shape). Live runs
go through the live reporter; this is just the synchronous equivalent,
now expressed through the public `InvocationReport` boundary."""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from agent_callstack import InvocationReport, ROOT_FRAME_KEY
from agent_callstack.driver import Tree


def write_invocation_report(
    *,
    log_dir: Path,
    invoke_id: str,
    kind: str,
    tasks: Sequence[str],
    tree: Tree,
    cwd: str,
    started_at: str,
    ended_at: str,
) -> Path:
    """Materialize a root frame + merged report in one synchronous pass.

    The production path lives in the live reporter (`InvocationReport.reporter`);
    this helper preserves the older one-shot behaviour for tests that want to
    assert on the final merged document without running the debounce timer."""
    report = InvocationReport(invoke_id=invoke_id, log_dir=log_dir, cwd=cwd)
    report.write_frame({
        "frame_key": ROOT_FRAME_KEY, "is_nested": False,
        "kind": kind, "tasks": list(tasks), "cwd": cwd,
        "started_at": started_at, "ended_at": ended_at,
        "tree": tree.to_dict(),
    })
    report.write_report(ended_at=ended_at)
    return report.report_path
