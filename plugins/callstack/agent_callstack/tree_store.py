"""Execution-tree snapshot persistence (resume sidecars).

`TreeStore` snapshots the full execution tree to a `.call_tree` sidecar next to
a yielded leaf's clone path so `resume` can reconstruct it. Split out of
`trace.py` (which owns the unrelated append-only `TraceWriter`): the two share
no state and have orthogonal lifetimes — TraceWriter appends continuously,
TreeStore saves once on yield and consumes once on resume. Re-exported from
`trace` for backward-compatible imports.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Optional


class TreeStore:
    """Persist execution-tree snapshots as `<clone>.call_tree` JSON files.

    Loading also deletes the sidecar — resume consumes it. If the resumed
    session yields again the driver writes a fresh snapshot."""

    @staticmethod
    def _path_for(clone_path: Path) -> Path:
        return Path(str(clone_path) + ".call_tree")

    def save(self, clone_path: Path, snapshot: dict) -> Path:
        path = self._path_for(clone_path)
        with open(path, "w") as f:
            json.dump(snapshot, f, indent=2, default=_json_default)
        return path

    def load(self, clone_path: Path) -> Optional[dict]:
        """Consume the sidecar atomically.

        SEC-007: the prior `exists → open → unlink` sequence let two
        concurrent resumes both pass `exists`, both read the same dict,
        and then the second `unlink` would raise. Now we rename the
        sidecar to a unique claim file first; whoever wins the rename
        owns the read. The loser sees `FileNotFoundError` from
        `os.replace` and returns None."""
        path = self._path_for(clone_path)
        claim = Path(f"{path}.claim-{uuid.uuid4().hex[:8]}")
        try:
            os.replace(path, claim)
        except FileNotFoundError:
            return None
        try:
            with open(claim, "r") as f:
                data = json.load(f)
        finally:
            with contextlib.suppress(FileNotFoundError):
                claim.unlink()
        return data


def _json_default(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
