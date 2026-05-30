"""Persistence: append-only call traces.

- TraceWriter: appends one JSONL line per turn for forensic replay.

`TreeStore` (execution-tree resume sidecars) lives in `tree_store.py` — an
orthogonal concern with no shared state — and is re-exported below so existing
`from agent_callstack.trace import TreeStore` imports keep working.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from .tree_store import TreeStore  # noqa: F401  (re-export for backward-compatible imports)


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
