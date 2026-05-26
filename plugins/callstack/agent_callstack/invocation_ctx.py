"""Where a Caller writes its per-invocation artifacts.

`_InvocationContext` carries the paths + ids that the reporter and the
trace writer both need, plus the `frame_key` that pins this invocation's
frame file to a node id (so the merged report can graft nested frames
under the right caller node).

For the root (top-level) invocation `frame_key == "root"` and the Caller
owns the full invocation directory. For a nested MCP call — detected via
`CALLSTACK_ROOT_*` env — the Caller reuses the root's invocation directory
and writes its own tree to `_frames/{caller_session}.yaml`, where the
root's `_LiveReporter` picks it up and grafts it under the caller's node.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _new_invoke_id() -> str:
    """Sortable, collision-resistant id: `YYYYMMDDTHHMMSS-<8 hex>`."""
    return f"{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


@dataclass(frozen=True)
class _InvocationContext:
    """Where a Caller writes its per-invocation artifacts.

    ``instance_id`` disambiguates the frame *file* when multiple nested
    invocations share the same ``frame_key`` (i.e. the same caller node
    issues several sibling ``invoke*`` calls). Empty string preserves the
    legacy ``{frame_key}.yaml`` filename — used for the root frame and for
    tests that construct the context directly without setting it.

    For the root (top-level) invocation `frame_key == "root"` and the Caller
    owns the full invocation directory. For a nested MCP call — detected via
    `CALLSTACK_ROOT_*` env — the Caller reuses the root's invocation directory
    and writes its own tree to `_frames/{caller_session}.yaml`, where the
    root's `_LiveReporter` picks it up and grafts it under the caller's node."""

    invoke_id: str
    log_dir: Path
    cwd: str
    frame_key: str
    is_nested: bool
    instance_id: str = ""

    @property
    def invocation_dir(self) -> Path:
        return self.log_dir / self.invoke_id

    @property
    def frames_dir(self) -> Path:
        return self.invocation_dir / "_frames"

    @property
    def report_path(self) -> Path:
        return self.invocation_dir / "report.yaml"

    @property
    def log_path(self) -> Path:
        return self.invocation_dir / "progress.log"

    @property
    def lock_path(self) -> Path:
        return self.invocation_dir / ".report.lock"

    def frame_path(self, key: Optional[str] = None) -> Path:
        # Explicit key override (used by callers that need to read a peer
        # frame) — keep the legacy single-file filename.
        if key is not None:
            return self.frames_dir / f"{key}.yaml"
        # Production nested invocations carry a unique ``instance_id`` so
        # multiple sibling invokes from the same caller don't overwrite
        # each other's frame. The frame's ``frame_key`` field still pins it
        # to the caller node for grafting.
        if self.instance_id:
            return self.frames_dir / f"{self.frame_key}-{self.instance_id}.yaml"
        return self.frames_dir / f"{self.frame_key}.yaml"

    def prefix(self, kind: str) -> str:
        return f"nested_{kind}" if self.is_nested else kind
