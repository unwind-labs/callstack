"""Public value types + tree → public result translation.

Exposes `Result`, `MultiResult`, `YieldToken`, `CallYielded`, `CallFailed`
— the surface that `Caller.call() / .call_many() / .resume()` returns or
raises. Also owns the private translators that map a finished Tree's
nodes into those types (used by Caller and re-exported for the MCP
server / tests).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .driver import Node, Tree

# ---------- Helpers ----------


def _find_task_start_line(log_path: Path, task_id: str) -> Optional[int]:
    """1-based line in `log_path` where this node's task begins.

    Scans for `## Starting Task [<task_id>]` and returns the LAST match.
    Claude Code's CLI writes the prompt twice into a forked JSONL — once
    as a `queue-operation` bookkeeping row near the top, then again as
    the actual `user` message after the inherited transcript replays.
    The model sees the user message, so the last occurrence is the
    meaningful "child's work starts here" pointer.

    Returns None if the file is unreadable or the marker is absent;
    callers fall back to the approximate `parent_lines + 1`.
    """
    marker = f"## Starting Task [{task_id}]"
    try:
        last: Optional[int] = None
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, start=1):
                if marker in line:
                    last = i
        return last
    except OSError:
        return None


# ---------- Public value types ----------


@dataclass(frozen=True)
class YieldToken:
    """Opaque handle for resuming a yielded session. Pass to `resume()`."""

    session_id: str
    clone_path: str


@dataclass(frozen=True)
class Result:
    value: Any
    summary: Optional[str]
    next: Optional[str]
    duration: float
    log: Optional[Path]
    log_start: int

    def to_envelope(self) -> dict:
        """Wire-format envelope returned by the MCP `call`/`resume` tools."""
        return {
            "status": "complete",
            "result": self.value,
            "summary": self.summary,
            "suggested_next": self.next,
            "duration": self.duration,
            "session_log": str(self.log) if self.log else None,
            "session_log_start_line": self.log_start,
        }


@dataclass(frozen=True)
class MultiResult:
    """Returned by `call_many` (mixed completes/errors/yields)."""

    results: list  # list[Result | CallFailed | CallYielded]


class CallYielded(Exception):
    """Raised when an agent emits YIELD. Carries the resume token + question."""

    def __init__(self, question: str, token: YieldToken):
        super().__init__(question)
        self.question = question
        self.token = token

    def to_envelope(self) -> dict:
        return {
            "status": "yield",
            "question": self.question,
            "session_id": self.token.session_id,
            "clone_path": self.token.clone_path,
        }


class CallFailed(Exception):
    """Raised when an agent or its descendants fail. Carries any partial output."""

    def __init__(self, error: str, partial: Any = None):
        super().__init__(error)
        self.error = error
        self.partial = partial

    def to_envelope(self) -> dict:
        return {
            "status": "error",
            "error": self.error,
            "partial_result": self.partial,
        }


# ---------- Tree → public result translation ----------


def _result_from_node(node: Node):
    """Convert a finished node into Result / CallYielded / CallFailed."""
    s = node.state
    if s.kind == "done":
        log_path = Path(node.clone_path) if node.clone_path else None
        # Prefer a precise scan for `## Starting Task [<id>]` over the
        # approximate parent_lines count: the parent's file length doesn't
        # line up exactly with where the new turn lands in the child's file
        # (CLI bookkeeping + replay re-encoding both shift the offset).
        precise = _find_task_start_line(log_path, node.id[:8]) if log_path else None
        return Result(
            value=node.result,
            summary=node.summary,
            next=node.suggested_next,
            duration=round(node.duration, 2),
            log=log_path,
            log_start=precise if precise is not None else node.parent_lines + 1,
        )
    if s.kind == "failed":
        return CallFailed(error=node.error or "unknown error", partial=node.result)
    if s.kind in ("timeout", "abandoned"):
        # Both are legitimate TERMINAL states a top-level node can carry by
        # the time results are extracted, so neither is "unexpected":
        #   - timeout:   report.seal() runs terminal_wait.expire_to_timeout
        #                BEFORE Caller extracts results (__init__.py ~224/274),
        #                stamping Timeout on any node still waiting for a late
        #                terminal envelope.
        #   - abandoned: orphan reconciliation (crashed writer pid) or shutdown
        #                hardening (atexit/SIGTERM/SIGINT) seals in-flight nodes.
        # Surface the state's own error so the caller sees WHY it ended, not a
        # synthetic "unexpected state" string that drops the real message.
        return CallFailed(error=node.error or s.error, partial=node.result)  # type: ignore[union-attr]
    if s.kind == "awaiting_user":
        # Wraps the leaf the user must answer.
        return CallYielded(
            question=node.state.question,  # type: ignore[union-attr]
            token=YieldToken(session_id=node.session_id or "", clone_path=node.clone_path or ""),
        )
    # Genuinely unreachable: TERMINAL == done/failed/timeout/abandoned (all
    # handled above) plus awaiting_user; drive() never returns an in-flight node.
    return CallFailed(error=f"node ended in unexpected state: {s.kind}")


def _results_from_tree(tree: Tree) -> list:
    out = []
    for root in tree.nodes:
        leaf = root.yielded_descendant()
        if leaf is not None:
            out.append(_result_from_node(leaf))
        else:
            out.append(_result_from_node(root))
    return out


def _unwrap_single(item) -> Result:
    if isinstance(item, Result):
        return item
    if isinstance(item, (CallYielded, CallFailed)):
        raise item
    raise CallFailed(error=f"unexpected result type: {type(item).__name__}")
