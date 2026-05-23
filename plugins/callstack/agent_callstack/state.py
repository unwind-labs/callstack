"""Pure state machine for one agent execution node.

A node moves through states in response to events. Every transition is a
pure function: `step(state, event) -> (new_state, [effects])`. Side effects
(spawning subprocesses, writing disk) are described as Effect values and
performed by the driver — never inside `step`.

Each node runs its own state machine. The tree topology and child→parent
result propagation are the driver's job; this module only knows about a
single node.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional, Union

from .protocol import (
    Call,
    Envelope,
    Return,
    Yield,
    child_returned_prompt,
    starting_prompt,
)


# ---------- States ----------

@dataclass(frozen=True)
class Pending:
    """Node created, not yet started. Carries what's needed to issue first turn."""
    parent_session_id: str
    task: str
    task_id: Optional[str] = None
    # How the first turn launches its underlying `claude` session:
    #   "fork"  — `--resume <parent> --fork-session` (inherits parent transcript)
    #   "fresh" — neither flag (brand-new session, no inherited context)
    context_mode: Literal["fork", "fresh"] = "fork"
    kind: Literal["pending"] = "pending"


@dataclass(frozen=True)
class AwaitingTurn:
    """An LLM turn is in flight. session_id is None on the very first turn
    (we're forking and the new id will arrive with TurnCompleted)."""
    session_id: Optional[str]
    kind: Literal["awaiting_turn"] = "awaiting_turn"


@dataclass(frozen=True)
class AwaitingChild:
    """A CALL is in flight. We're paused until the child terminates."""
    session_id: str
    child_id: str
    kind: Literal["awaiting_child"] = "awaiting_child"


@dataclass(frozen=True)
class AwaitingUser:
    """The agent YIELDed. Suspended until the user replies."""
    session_id: str
    question: str
    kind: Literal["awaiting_user"] = "awaiting_user"


@dataclass(frozen=True)
class Done:
    session_id: Optional[str] = None
    result: Any = None
    summary: Optional[str] = None
    suggested_next: Optional[str] = None
    kind: Literal["done"] = "done"


@dataclass(frozen=True)
class Failed:
    error: str
    session_id: Optional[str] = None
    partial: Any = None
    kind: Literal["failed"] = "failed"


@dataclass(frozen=True)
class Timeout:
    """Terminal state recorded when finalize() waited for a late terminal
    envelope on the child JSONL and never received one. Distinct from
    Failed so reports can distinguish "the child errored" from "we gave
    up waiting for the child's envelope"."""
    error: str = "wait-for-terminal-envelope budget elapsed"
    session_id: Optional[str] = None
    kind: Literal["timeout"] = "timeout"


@dataclass(frozen=True)
class Abandoned:
    """Synthetic terminal state used when an in-flight node must be
    sealed by something *other* than its own state machine. Two producers:

    1. Orphan reconciliation (frames._reconcile_orphan_states), when a
       frame's writer pid is no longer alive (the writer crashed before
       it could record its own terminal envelope).
    2. Shutdown hardening (reporter._flush_active_reporters), when the
       process is being torn down (atexit / SIGTERM / SIGINT) with
       non-terminal nodes still in flight.

    Distinct from Failed because no LLM-side error occurred — the driver
    simply never got the chance to record the terminal envelope. Distinct
    from Timeout because we did not even wait for a JSONL envelope; the
    decision was made by an external signal."""
    error: str
    session_id: Optional[str] = None
    kind: Literal["abandoned"] = "abandoned"


State = Union[Pending, AwaitingTurn, AwaitingChild, AwaitingUser, Done, Failed,
              Timeout, Abandoned]

TERMINAL = ("done", "failed", "timeout", "abandoned")
SUSPENDED = ("awaiting_user",)  # node is parked, waiting for an out-of-band event


def is_terminal(state: State) -> bool:
    return state.kind in TERMINAL


def is_suspended(state: State) -> bool:
    return state.kind in SUSPENDED


def is_eligible_for_abandonment(kind: str) -> bool:
    """REVIEW-201: single policy shared by every "seal non-terminal nodes
    when something went wrong upstream" code path (frames orphan
    reconciliation, MCP boundary guard, shutdown/signal handler).

    Returns True iff `kind` is neither already terminal (nothing to do)
    nor SUSPENDED — ``AwaitingUser`` is legitimately parked waiting for a
    user reply; sealing it as abandoned would destroy that intent.

    Takes a state-kind string so it works against both the Tree-shape
    (``state.kind`` via ``getattr``) and the dict-shape (``state["kind"]``)
    walks. Both walkers consult this predicate so they can't drift."""
    return kind not in TERMINAL and kind not in SUSPENDED


# Canonical mapping from state kind → externally-visible status label.
# Owned here because state.py is the source of truth for state kinds;
# both the runtime (driver.Node.status) and the merged report (frames.py)
# consume the same labels.
#
# "abandoned" is a synthetic terminal kind never produced by step(); the
# merge layer (frames.py) rewrites a frame's non-terminal node states to
# this when the frame's owning writer process is no longer alive. Distinct
# from "failed" so consumers can tell "the agent errored" from "the writer
# died before producing a terminal envelope."
_STATUS_BY_KIND = {
    "pending": "pending",
    "awaiting_turn": "running",
    "awaiting_child": "running",
    "awaiting_user": "yielded",
    "done": "complete",
    "failed": "error",
    "timeout": "timeout",
    "abandoned": "abandoned",
}


def status_label(state: object) -> str:
    """Map a State instance or a `{state: ..., kind: ...}` dict to its
    user-facing status label. Unknown kinds → ``"unknown"``."""
    kind: object
    if isinstance(state, dict):
        kind = state.get("kind", "")
    else:
        kind = getattr(state, "kind", "")
    return _STATUS_BY_KIND.get(kind, "unknown") if isinstance(kind, str) else "unknown"


# ---------- Events ----------

@dataclass(frozen=True)
class Start:
    """Begin executing a Pending node — issues the first (forked) turn."""


@dataclass(frozen=True)
class TurnCompleted:
    """An LLM turn produced an envelope; carries the (possibly new) session id."""
    envelope: Envelope
    session_id: str


@dataclass(frozen=True)
class TurnFailed:
    """An LLM turn raised (timeout, subprocess error)."""
    error: str
    session_id: Optional[str] = None
    partial: Any = None


@dataclass(frozen=True)
class ChildDone:
    child_id: str
    result: Any


@dataclass(frozen=True)
class ChildFailed:
    child_id: str
    error: str


@dataclass(frozen=True)
class UserReplied:
    reply: str


Event = Union[Start, TurnCompleted, TurnFailed, ChildDone, ChildFailed, UserReplied]


# ---------- Effects ----------

@dataclass(frozen=True)
class RunTurn:
    """Run an LLM turn against the channel.

    mode="fork":   `--resume <source> --fork-session` — inherits parent transcript,
                   then diverges into a new session id (reported via TurnCompleted).
    mode="fresh":  neither flag — brand-new session, no inherited context. The new
                   session id is reported via TurnCompleted.
    mode="resume": `--resume <source>` only — appends `prompt` to an existing session.
    """
    source_session_id: str
    prompt: str
    mode: Literal["fork", "fresh", "resume"]


@dataclass(frozen=True)
class SpawnChild:
    """Drive a child node forked from `parent_session_id`. Eventually emits
    ChildDone or ChildFailed back to this node."""
    parent_session_id: str
    task: str


Effect = Union[RunTurn, SpawnChild]


# ---------- Pure transition ----------

def step(state: State, event: Event) -> tuple[State, list[Effect]]:
    """Compute the next state and the effects to perform.

    Unknown (state, event) combinations raise — they indicate a driver bug
    (firing the wrong event for the current state)."""
    match (state, event):
        # ---- Pending: kick off the first turn ----
        case (Pending(parent_session_id=psid, task=task, task_id=tid,
                      context_mode=cmode), Start()):
            return (
                AwaitingTurn(session_id=None),
                [RunTurn(source_session_id=psid,
                         prompt=starting_prompt(task, tid),
                         mode=cmode)],
            )

        # ---- AwaitingTurn: an envelope arrived ----
        case (AwaitingTurn(), TurnCompleted(envelope=Return() as r, session_id=sid)):
            return (Done(session_id=sid, result=r.result,
                         summary=r.summary, suggested_next=r.suggested_next),
                    [])

        case (AwaitingTurn(), TurnCompleted(envelope=Yield(question=q), session_id=sid)):
            return (AwaitingUser(session_id=sid, question=q), [])

        case (AwaitingTurn(), TurnCompleted(envelope=Call(task=t), session_id=sid)):
            child_id = _fresh_child_id()
            return (AwaitingChild(session_id=sid, child_id=child_id),
                    [SpawnChild(parent_session_id=sid, task=t)])

        case (AwaitingTurn(session_id=sid), TurnFailed(error=e, session_id=tsid, partial=p)):
            return (Failed(error=e, session_id=tsid or sid, partial=p), [])

        # ---- AwaitingChild: child terminated, resume ourselves ----
        case (AwaitingChild(session_id=sid, child_id=cid),
              ChildDone(child_id=ec, result=res)) if cid == ec:
            return (
                AwaitingTurn(session_id=sid),
                [RunTurn(source_session_id=sid,
                         prompt=child_returned_prompt(res),
                         mode="resume")],
            )

        case (AwaitingChild(session_id=sid, child_id=cid),
              ChildFailed(child_id=ec, error=err)) if cid == ec:
            return (Failed(error=f"Child failed: {err}", session_id=sid), [])

        # ---- AwaitingUser: user replied, resume the agent ----
        case (AwaitingUser(session_id=sid), UserReplied(reply=r)):
            return (
                AwaitingTurn(session_id=sid),
                [RunTurn(source_session_id=sid, prompt=r, mode="resume")],
            )

    raise AssertionError(f"no transition: {type(state).__name__} <- {type(event).__name__}")


def _fresh_child_id() -> str:
    import uuid
    return uuid.uuid4().hex
