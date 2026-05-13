"""Test-only helpers (ARCH-13: legacy one-shot report writer used by
tests for back-to-back assertions of merged-report shape). Live runs
go through `_LiveReporter`; this is just the synchronous equivalent."""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from agent_callstack import _InvocationContext, _ROOT_FRAME_KEY
from agent_callstack.driver import Tree
from agent_callstack.frames import _build_merged_report, _load_frames
from agent_callstack.reporter import _atomic_yaml_write


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

    The production path lives in `_LiveReporter`; this helper preserves
    the older one-shot behaviour for tests that want to assert on the
    final merged document without running the debounce timer."""
    ctx = _InvocationContext(
        invoke_id=invoke_id, log_dir=log_dir, cwd=cwd,
        frame_key=_ROOT_FRAME_KEY, is_nested=False,
    )
    ctx.frames_dir.mkdir(parents=True, exist_ok=True)
    _atomic_yaml_write(ctx.frame_path(), {
        "frame_key": _ROOT_FRAME_KEY, "is_nested": False,
        "kind": kind, "tasks": list(tasks), "cwd": cwd,
        "started_at": started_at, "ended_at": ended_at,
        "tree": tree.to_dict(),
    })
    frames = _load_frames(ctx.frames_dir)
    doc = _build_merged_report(
        invoke_id=invoke_id, frames=frames,
        root_frame=frames[_ROOT_FRAME_KEY][0], ended_at=ended_at,
    )
    _atomic_yaml_write(ctx.report_path, doc)
    return ctx.report_path
