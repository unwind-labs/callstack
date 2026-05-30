"""One node-sealing operation shared by every external finalize trigger.

A node normally reaches a terminal state through the pure `state.step`. But
four triggers must seal a non-terminal node when its own state machine can't:

  * terminal-wait budget expiry      -> Timeout  (we waited, gave up)
  * shutdown emergency (atexit/sig)  -> Abandoned (external signal sealed us)
  * background-call crash recovery   -> Abandoned
  * orphan reconciliation (dead pid) -> Abandoned

These previously had two duplicated walkers — a Tree-shape one in `reporter`
and a dict-shape one in `frames` — kept in policy lockstep only by convention.
This module is the single walk-mutate-recurse, parameterized by:

  * the terminal *cause* (Timeout vs Abandoned — the only thing that varies),
  * a `NodeView` adapter that hides whether the backing node is an in-memory
    `driver.Node` or a serialized frame dict.

The principled Timeout-vs-Abandoned distinction lives in the `Cause` the
trigger picks; the core only applies it. `state.is_eligible_for_abandonment`
remains the single eligibility policy, consulted here in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Literal, Optional, Protocol, Union

from . import state as st

# ---------- the termination cause ----------


@dataclass(frozen=True)
class AbandonCause:
    """Seal eligible nodes as `state.Abandoned`. The driver never got to record
    a terminal envelope (writer died / shutdown / dead-writer reconciliation).
    The reason is stamped into the error along with the prior state kind."""

    reason: str
    kind: Literal["abandon"] = "abandon"


@dataclass(frozen=True)
class TimeoutCause:
    """Seal eligible nodes as `state.Timeout`. We waited for a late terminal
    envelope on the child JSONL and the budget elapsed."""

    error: str = "wait-for-terminal-envelope budget elapsed"
    kind: Literal["timeout"] = "timeout"


Cause = Union[AbandonCause, TimeoutCause]


def terminal_state_for(cause: Cause, *, prior_kind: str, session_id: Optional[str]) -> st.State:
    """The single map from a (cause, prior-kind) pair to the terminal State a
    sealed node should hold. Abandon stamps the prior kind into the message
    (so post-mortems see *what* stopped advancing); Timeout uses its fixed
    wait-budget message verbatim."""
    if cause.kind == "abandon":
        return st.Abandoned(error=f"{cause.reason} (state was {prior_kind!r})", session_id=session_id)
    return st.Timeout(error=cause.error, session_id=session_id)


# ---------- shape-agnostic node view ----------


class NodeView(Protocol):
    """A uniform read/write view over one node, hiding Tree-shape vs dict-shape.
    `state_kind()` returns None when the node carries no usable state (a
    malformed frame dict) — such a node is skipped but its children are still
    walked, matching the original dict walker's tolerance."""

    def state_kind(self) -> Optional[str]: ...
    def session_id(self) -> Optional[str]: ...
    def seal(self, terminal: st.State) -> None: ...
    def children(self) -> "List[NodeView]": ...


def seal_tree(roots: "List[NodeView]", cause: Cause) -> int:
    """Walk every node reachable from `roots`; seal each eligible non-terminal
    node to the terminal state implied by `cause`. Returns the count sealed.
    Idempotent (already-terminal / parked nodes are skipped, so re-running is a
    no-op) and never raises on shape (the adapters tolerate malformed input)."""
    changed = 0
    for v in roots:
        changed += _seal_one(v, cause)
    return changed


def _seal_one(v: NodeView, cause: Cause) -> int:
    changed = 0
    kind = v.state_kind()
    if kind is not None and st.is_eligible_for_abandonment(kind):
        v.seal(terminal_state_for(cause, prior_kind=kind, session_id=v.session_id()))
        changed += 1
    for c in v.children():
        changed += _seal_one(c, cause)
    return changed


# ---------- adapters (no imports of driver/frames — pure duck typing) ----------


class _TreeNodeView:
    """Adapter over an in-memory `driver.Node`. `seal` is a single assignment to
    `node.state`; the Node's derived `error`/`session_id` properties follow."""

    __slots__ = ("_node",)

    def __init__(self, node: Any) -> None:
        self._node = node

    def state_kind(self) -> Optional[str]:
        return self._node.state.kind

    def session_id(self) -> Optional[str]:
        return getattr(self._node.state, "session_id", None)

    def seal(self, terminal: st.State) -> None:
        self._node.state = terminal

    def children(self) -> "List[NodeView]":
        return [_TreeNodeView(c) for c in self._node.children]


class _DictNodeView:
    """Adapter over a serialized frame node dict (`Node.to_dict()` shape)."""

    __slots__ = ("_node",)

    def __init__(self, node: dict) -> None:
        self._node = node

    def state_kind(self) -> Optional[str]:
        s = self._node.get("state")
        if not isinstance(s, dict):
            return None
        kind = s.get("kind")
        return kind if isinstance(kind, str) else None

    def session_id(self) -> Optional[str]:
        s = self._node.get("state")
        sid = s.get("session_id") if isinstance(s, dict) else None
        return sid or self._node.get("session_id")

    def seal(self, terminal: st.State) -> None:
        err = getattr(terminal, "error", None)
        new_state: dict = {"kind": terminal.kind, "error": err}
        sid = getattr(terminal, "session_id", None)
        if sid:
            new_state["session_id"] = sid
        self._node["state"] = new_state
        # Mirror onto the top-level error only if absent — never clobber an
        # error the node already carried.
        if not self._node.get("error"):
            self._node["error"] = err

    def children(self) -> "List[NodeView]":
        ch = self._node.get("children")
        return tree_dict_views(ch) if isinstance(ch, list) else []


def tree_views(nodes: Any) -> "List[NodeView]":
    """Wrap in-memory `driver.Node`s (e.g. `tree.nodes`) as NodeViews."""
    return [_TreeNodeView(n) for n in nodes]


def tree_dict_views(nodes: Any) -> "List[NodeView]":
    """Wrap serialized frame node dicts as NodeViews, skipping non-dict
    entries (the dict walker's defensive tolerance)."""
    return [_DictNodeView(n) for n in nodes if isinstance(n, dict)]
