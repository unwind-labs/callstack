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
        return Result(
            value=node.result, summary=node.summary, next=node.suggested_next,
            duration=round(node.duration, 2),
            log=Path(node.clone_path) if node.clone_path else None,
            log_start=node.parent_lines + 1,
        )
    if s.kind == "failed":
        return CallFailed(error=node.error or "unknown error", partial=node.result)
    if s.kind == "awaiting_user":
        # Wraps the leaf the user must answer.
        return CallYielded(
            question=node.state.question,  # type: ignore[union-attr]
            token=YieldToken(session_id=node.session_id or "",
                             clone_path=node.clone_path or ""),
        )
    # Should not happen — drive() returned with an in-flight node.
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


def _wrap(item):
    """Identity for serialization; kept as a hook for future shaping."""
    return item
