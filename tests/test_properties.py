"""Property-based tests (hypothesis) for the three pure cores of the runtime.

Example-based tests pin specific shapes; these pin *invariants* that must hold
across the whole input space — the kind of thing a hand-written example set
silently under-covers. Three properties, one per pure module:

  1. protocol.parse_envelope — round-trips any envelope it can produce, and
     never raises on arbitrary text (a malformed child turn must degrade to a
     protocol violation, never crash the driver).
  2. state.step — across any random walk of *legal* transitions the documented
     invariants hold: session id never drifts once known, every reachable state
     has a user-facing status label, and terminal states absorb all events.
  3. env propagation — a cap a parent chooses in the supported range reaches a
     child unchanged (CORR-101: no silent revert to the default budget).
"""
from __future__ import annotations

import json
import os

from hypothesis import given, settings
from hypothesis import strategies as st

from agent_callstack import Caller
from agent_callstack import env
from agent_callstack.env import ENV_MAX_DEPTH, _DEFAULT_MAX_DEPTH, _MAX_DEPTH_CEILING
from agent_callstack.protocol import Call, Return, Yield, parse_envelope
from agent_callstack.state import (
    Abandoned,
    AwaitingChild,
    AwaitingTurn,
    AwaitingUser,
    ChildDone,
    ChildFailed,
    Done,
    Failed,
    Pending,
    RunTurn,
    SpawnChild,
    Start,
    Timeout,
    TurnCompleted,
    TurnFailed,
    UserReplied,
    is_terminal,
    status_label,
    step,
)


# ---------- shared strategies ----------

# JSON-roundtrippable values (the domain of Return.result / ChildDone.result).
# Floats are excluded deliberately: NaN breaks `==`, which would make the
# round-trip property spuriously fail on a value the protocol never needs.
# Dict keys are text because JSON object keys are always strings.
_json = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(),
    lambda children: (
        st.lists(children, max_size=3)
        | st.dictionaries(st.text(max_size=5), children, max_size=3)
    ),
    max_leaves=5,
)


# ---------- 1. protocol round-trip + crash-freedom ----------

def _encode(envelope) -> str:
    """The canonical fenced-JSON wire form for an envelope. protocol.py owns
    the decoder but no encoder, so the test owns the inverse it asserts
    against — using exactly the keys parse_envelope reads back."""
    if isinstance(envelope, Call):
        obj = {"op": "call", "task": envelope.task}
    elif isinstance(envelope, Yield):
        obj = {"op": "yield", "question": envelope.question}
    else:  # Return
        obj = {
            "op": "return",
            "result": envelope.result,
            "summary": envelope.summary,
            "next": envelope.suggested_next,
        }
    return "```json\n" + json.dumps(obj) + "\n```"


_envelopes = st.one_of(
    st.builds(Call, task=st.text()),
    st.builds(Yield, question=st.text()),
    st.builds(
        Return,
        result=_json,
        summary=st.none() | st.text(),
        suggested_next=st.none() | st.text(),
    ),
)

# Fragments that bias generated text toward the parser's interesting branches
# (fence open/close, op/keys, whole mini-envelopes) instead of mostly-random
# noise that never reaches the json.loads path.
_fence_noise = st.lists(
    st.sampled_from([
        "```json", "```", "\n", "{", "}", "[", "]", ":", ",", '"',
        '"op"', '"call"', '"yield"', '"return"', '"task"', '"question"',
        "garbage", '{"op":"call","task":"x"}', '{"op": "return"}',
        '{"op":"yield"}{"op":"return"}', "{not json}",
    ]),
    max_size=10,
).map("".join)


class TestProtocolRoundTrip:
    """parse_envelope is the only bridge between a child's raw stdout and the
    driver's control flow; if it loses or mangles an envelope the whole call
    tree mis-routes."""

    @given(_envelopes)
    @settings(max_examples=100, deadline=None)
    def test_encode_decode_is_identity(self, envelope):
        """Any envelope the protocol can produce survives a fenced-JSON
        round-trip unchanged — no field is dropped or reinterpreted."""
        assert parse_envelope(_encode(envelope)) == envelope

    @given(st.one_of(st.text(), _fence_noise))
    @settings(max_examples=200, deadline=None)
    def test_parse_never_raises_on_arbitrary_text(self, blob):
        """A malformed/garbage child turn must surface as a protocol
        violation (None) or a valid envelope — never an uncaught exception
        that would crash the driver mid-walk."""
        result = parse_envelope(blob)
        assert result is None or isinstance(result, (Call, Yield, Return))


# ---------- 2. state machine invariants ----------

_STATE_TYPES = (
    Pending, AwaitingTurn, AwaitingChild, AwaitingUser,
    Done, Failed, Timeout, Abandoned,
)
_EFFECT_TYPES = (RunTurn, SpawnChild)
_TERMINALS = [Done(), Failed(error="boom"), Timeout(), Abandoned(error="gone")]
_ALL_EVENTS = [
    Start(),
    TurnCompleted(envelope=Return(), session_id="s"),
    TurnFailed(error="e"),
    ChildDone(child_id="c", result=None),
    ChildFailed(child_id="c", error="e"),
    UserReplied(reply="r"),
]


class TestStateMachineInvariants:
    """step() is a pure transition the driver trusts blindly; an invariant
    break here (lost session id, an unlabelled state, a 'live' terminal) would
    corrupt the tree or the merged report without any local error."""

    @given(st.data())
    @settings(max_examples=100, deadline=None)
    def test_legal_walk_holds_invariants(self, data):
        """Walk a random sequence of legal events from Pending and assert,
        at every step: the result is a real State + list[Effect]; every
        reachable state maps to a known status label (so _STATUS_BY_KIND can
        never silently drift out of sync with the states step() produces);
        and a session id, once established, never changes under the
        resume/turn transitions (the driver keys everything off it)."""
        state = Pending(
            parent_session_id="root",
            task="t",
            context_mode=data.draw(st.sampled_from(["fork", "fresh"])),
        )
        established_sid = None

        for _ in range(12):  # bounded walk; most reach a terminal far sooner
            if is_terminal(state):
                break
            kind = state.kind
            if kind == "pending":
                event = Start()
            elif kind == "awaiting_turn":
                sid = established_sid or data.draw(
                    st.uuids().map(str))
                choice = data.draw(
                    st.sampled_from(["return", "yield", "call", "fail"]))
                if choice == "return":
                    event = TurnCompleted(Return(result=data.draw(_json)), sid)
                elif choice == "yield":
                    event = TurnCompleted(Yield(question=data.draw(st.text())), sid)
                elif choice == "call":
                    event = TurnCompleted(Call(task=data.draw(st.text())), sid)
                else:
                    # session_id=None so Failed inherits the established sid,
                    # exercising the `tsid or sid` fallback in step().
                    event = TurnFailed(error=data.draw(st.text()), session_id=None)
            elif kind == "awaiting_child":
                if data.draw(st.booleans()):
                    event = ChildDone(child_id=state.child_id,
                                      result=data.draw(_json))
                else:
                    event = ChildFailed(child_id=state.child_id,
                                        error=data.draw(st.text()))
            else:  # awaiting_user
                event = UserReplied(reply=data.draw(st.text()))

            new_state, effects = step(state, event)

            assert isinstance(new_state, _STATE_TYPES)
            assert isinstance(effects, list)
            assert all(isinstance(e, _EFFECT_TYPES) for e in effects)
            assert status_label(new_state) != "unknown"

            new_sid = getattr(new_state, "session_id", None)
            if new_sid is not None:
                if established_sid is None:
                    established_sid = new_sid
                else:
                    assert new_sid == established_sid, (
                        "session id drifted mid-walk; the driver keys child "
                        "resumption off a stable id")
            state = new_state

    @given(st.sampled_from(_TERMINALS), st.sampled_from(_ALL_EVENTS))
    @settings(max_examples=100, deadline=None)
    def test_terminal_states_absorb_every_event(self, terminal, event):
        """A terminal node has no successor: feeding it any event must fail
        loud (AssertionError from step's exhaustiveness guard), never silently
        resurrect it into a running state."""
        try:
            step(terminal, event)
        except AssertionError:
            return
        raise AssertionError(
            f"{terminal.kind} accepted {type(event).__name__} but terminal "
            f"states must reject all events")


# ---------- 3. env / cap propagation ----------

def _child_reads_max_depth(stamped: str) -> int:
    """Simulate the child process: read the stamped value back through the
    same env reader a spawned claude uses, restoring os.environ after."""
    prev = os.environ.get(ENV_MAX_DEPTH)
    os.environ[ENV_MAX_DEPTH] = stamped
    try:
        return env.max_depth()
    finally:
        if prev is None:
            os.environ.pop(ENV_MAX_DEPTH, None)
        else:
            os.environ[ENV_MAX_DEPTH] = prev


class TestCapPropagation:
    """CORR-101: the depth budget a parent chooses must reach grandchildren
    unchanged. The example test pins max_depth=3 and the default; this pins
    the whole supported range."""

    @given(st.one_of(st.none(), st.integers(min_value=1,
                                            max_value=_MAX_DEPTH_CEILING)))
    @settings(max_examples=100, deadline=None)
    def test_supported_cap_reaches_child_without_drift(self, n):
        """For any cap in the supported range (1.._MAX_DEPTH_CEILING) — and
        the unset/default case — the value stamped onto the child env, read
        back through env.max_depth(), equals the cap the parent chose. Outside
        this range the documented clamp/default applies (covered by env.py's
        own unit tests); the supported envelope must round-trip exactly."""
        expected = _DEFAULT_MAX_DEPTH if n is None else n
        caller = Caller(max_depth=n)
        ctx = caller._inv.context(None)
        stamped = caller._inv.child_env(ctx, depth_base=0)[ENV_MAX_DEPTH]

        assert stamped == str(expected), "parent stamped a cap it didn't choose"
        assert _child_reads_max_depth(stamped) == expected, (
            "child enforces a different cap than the parent chose — CORR-101 "
            "drift")
