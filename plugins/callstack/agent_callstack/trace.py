"""Persistence: append-only call traces and tree snapshots.

Two concerns, each owned by one class:
- TraceWriter: appends one JSONL line per turn for forensic replay.
- TreeStore: snapshots the full execution tree to a `.call_tree` sidecar
  next to a yielded leaf's clone path so resume can reconstruct it.
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Optional


class TraceWriter:
    """Append one line per turn to `<trace_dir>/call_trace.jsonl`."""

    def __init__(self, trace_dir: Path):
        self._dir = trace_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = self._dir / "call_trace.jsonl"
        # One TraceWriter is shared across the driver's worker threads, which
        # append concurrently. A single `f.write()` is not guaranteed to be one
        # syscall once the JSON line exceeds PIPE_BUF, so unsynchronized appends
        # can interleave and corrupt a line. Serialize writes; the writer is
        # single-process per invocation, so an in-process lock is sufficient
        # (no need for the fcntl.lockf the cross-process report merge uses).
        self._lock = threading.Lock()

    def write(
        self,
        *,
        depth: int,
        task: str,
        session_id: str,
        result: str,
        duration: float,
        api_request_id: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_creation_tokens: int,
        started_at_utc: str,
        ended_at_utc: str,
        seed: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        entry = {
            "timestamp": started_at_utc,
            "ended_at": ended_at_utc,
            "call_depth": depth,
            "session_id": session_id,
            "api_request_id": api_request_id,
            "task": task[:200],
            "duration_seconds": round(duration, 2),
            "result_length": len(result),
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read_tokens,
                "cache_creation_tokens": cache_creation_tokens,
            },
            "seed": seed,
            "error": error,
        }
        line = json.dumps(entry) + "\n"
        with self._lock:
            # Re-ensure the dir exists: external processes may prune sibling
            # directories in the trace parent (we've seen this under
            # ~/.claude/projects/<proj>/), so mkdir-at-init alone isn't enough.
            self._dir.mkdir(parents=True, exist_ok=True)
            with open(self._file, "a") as f:
                f.write(line)


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
