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


State = Union[Pending, AwaitingTurn, AwaitingChild, AwaitingUser, Done, Failed]

TERMINAL = ("done", "failed")
SUSPENDED = ("awaiting_user",)  # node is parked, waiting for an out-of-band event


def is_terminal(state: State) -> bool:
    return state.kind in TERMINAL


def is_suspended(state: State) -> bool:
    return state.kind in SUSPENDED


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
