"""Persistence: append-only call traces and tree snapshots.

Two concerns, each owned by one class:
- TraceWriter: appends one JSONL line per turn for forensic replay.
- TreeStore: snapshots the full execution tree to a `.call_tree` sidecar
  next to a yielded leaf's clone path so resume can reconstruct it.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Optional


class TraceWriter:
    """Append one line per turn to `<trace_dir>/call_trace.jsonl`."""

    def __init__(self, trace_dir: Path):
        self._dir = trace_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = self._dir / "call_trace.jsonl"

    def write(
        self,
        *,
        depth: int,
        task: str,
        session_id: str,
        result: str,
        duration: float,
        error: Optional[str] = None,
    ) -> None:
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "call_depth": depth,
            "session_id": session_id,
            "task": task[:200],
            "duration_seconds": round(duration, 2),
            "result_length": len(result),
            "error": error,
        }
        with open(self._file, "a") as f:
            f.write(json.dumps(entry) + "\n")


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
        path = self._path_for(clone_path)
        if not path.exists():
            return None
        with open(path, "r") as f:
            data = json.load(f)
        path.unlink()
        return data


def _json_default(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
