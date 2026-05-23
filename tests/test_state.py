"""Tests for the pure state machine: every (state, event) → (new_state, [effects])."""
from __future__ import annotations

import pytest

from agent_callstack import state as st
from agent_callstack.protocol import Call, Return, Yield


# ---------- helpers ----------

def _start_node(parent="parent-sess", task="do thing", task_id="n1"):
    return st.Pending(parent_session_id=parent, task=task, task_id=task_id)


# ---------- Pending → AwaitingTurn ----------

class TestStart:

    def test_pending_to_awaiting_turn_via_runturn(self):
        new_state, effects = st.step(_start_node(), st.Start())
        assert isinstance(new_state, st.AwaitingTurn)
        assert new_state.session_id is None
        assert len(effects) == 1
        eff = effects[0]
        assert isinstance(eff, st.RunTurn)
        assert eff.mode == "fork"
        assert eff.source_session_id == "parent-sess"
        assert "do thing" in eff.prompt
        assert "[n1]" in eff.prompt

    def test_pending_with_fresh_context_emits_fresh_runturn(self):
        node = st.Pending(parent_session_id="parent-sess", task="do thing",
                          task_id="n1", context_mode="fresh")
        _, effects = st.step(node, st.Start())
        eff = effects[0]
        assert isinstance(eff, st.RunTurn)
        assert eff.mode == "fresh"

    def test_pending_default_context_mode_is_fork(self):
        # Backward compat: Pending without context_mode kwarg behaves as fork.
        node = st.Pending(parent_session_id="p", task="t")
        _, effects = st.step(node, st.Start())
        assert effects[0].mode == "fork"


# ---------- AwaitingTurn → terminal/intermediate ----------

class TestTurnCompleted:

    def test_return_terminates(self):
        new_state, effects = st.step(
            st.AwaitingTurn(session_id=None),
            st.TurnCompleted(envelope=Return(result="ok", summary="s", suggested_next="n"),
                             session_id="forked"),
        )
        assert isinstance(new_state, st.Done)
        assert new_state.session_id == "forked"
        assert new_state.result == "ok"
        assert new_state.summary == "s"
        assert new_state.suggested_next == "n"
        assert effects == []

    def test_yield_suspends(self):
        new_state, effects = st.step(
            st.AwaitingTurn(session_id="sess"),
            st.TurnCompleted(envelope=Yield(question="MFA?"), session_id="sess"),
        )
        assert new_state == st.AwaitingUser(session_id="sess", question="MFA?")
        assert effects == []

    def test_call_spawns_child(self):
        new_state, effects = st.step(
            st.AwaitingTurn(session_id="sess"),
            st.TurnCompleted(envelope=Call(task="sub"), session_id="sess"),
        )
        assert isinstance(new_state, st.AwaitingChild)
        assert new_state.session_id == "sess"
        assert len(effects) == 1
        assert isinstance(effects[0], st.SpawnChild)
        assert effects[0].parent_session_id == "sess"
        assert effects[0].task == "sub"

    def test_turn_failed(self):
        new_state, effects = st.step(
            st.AwaitingTurn(session_id="sess"),
            st.TurnFailed(error="boom", partial="some"),
        )
        assert new_state == st.Failed(error="boom", session_id="sess", partial="some")
        assert effects == []

    def test_turn_failed_uses_event_session_id_when_present(self):
        new_state, _ = st.step(
            st.AwaitingTurn(session_id=None),
            st.TurnFailed(error="boom", session_id="from-event"),
        )
        assert isinstance(new_state, st.Failed)
        assert new_state.session_id == "from-event"


# ---------- AwaitingChild → resume self ----------

class TestChildEvents:

    def _state(self, child_id="c1"):
        return st.AwaitingChild(session_id="sess", child_id=child_id)

    def test_child_done_resumes_with_result_prompt(self):
        new_state, effects = st.step(self._state(), st.ChildDone(child_id="c1", result="data"))
        assert new_state == st.AwaitingTurn(session_id="sess")
        assert len(effects) == 1
        eff = effects[0]
        assert isinstance(eff, st.RunTurn)
        assert eff.mode == "resume"
        assert eff.source_session_id == "sess"
        assert "Your child completed" in eff.prompt
        assert "data" in eff.prompt

    def test_child_done_with_dict_result(self):
        _, effects = st.step(self._state(), st.ChildDone(child_id="c1", result={"x": 1}))
        assert '{"x": 1}' in effects[0].prompt

    def test_child_failed_propagates(self):
        new_state, effects = st.step(self._state(), st.ChildFailed(child_id="c1", error="oops"))
        assert isinstance(new_state, st.Failed)
        assert "oops" in new_state.error
        assert new_state.session_id == "sess"
        assert effects == []

    def test_mismatched_child_id_raises(self):
        with pytest.raises(AssertionError):
            st.step(self._state("c1"), st.ChildDone(child_id="other", result="x"))


# ---------- AwaitingUser → resume agent ----------

class TestUserResume:

    def test_user_replied_resumes(self):
        new_state, effects = st.step(
            st.AwaitingUser(session_id="sess", question="?"),
            st.UserReplied(reply="847291"),
        )
        assert new_state == st.AwaitingTurn(session_id="sess")
        assert len(effects) == 1
        eff = effects[0]
        assert isinstance(eff, st.RunTurn)
        assert eff.mode == "resume"
        assert eff.source_session_id == "sess"
        assert eff.prompt == "847291"


# ---------- Predicates ----------

class TestPredicates:

    def test_terminal_states(self):
        assert st.is_terminal(st.Done())
        assert st.is_terminal(st.Failed(error="x"))
        assert not st.is_terminal(st.Pending(parent_session_id="", task=""))
        assert not st.is_terminal(st.AwaitingTurn(session_id=None))
        assert not st.is_terminal(st.AwaitingUser(session_id="s", question="?"))

    def test_suspended_states(self):
        assert st.is_suspended(st.AwaitingUser(session_id="s", question="?"))
        assert not st.is_suspended(st.Done())
        assert not st.is_suspended(st.AwaitingChild(session_id="s", child_id="c"))

    def test_eligible_for_abandonment(self):
        """REVIEW-201: single shared policy used by the dict-shape and
        Tree-shape abandonment walkers. Terminal kinds → False (nothing
        to do). SUSPENDED kinds → False (AwaitingUser must NOT be
        silently sealed; the previous dict walker had this bug).
        Non-terminal non-suspended → True."""
        # Terminal — already sealed.
        for kind in ("done", "failed", "timeout", "abandoned"):
            assert not st.is_eligible_for_abandonment(kind), kind
        # Suspended — legitimately parked, must NOT be demoted.
        assert not st.is_eligible_for_abandonment("awaiting_user")
        # Non-terminal in-flight kinds — eligible.
        for kind in ("pending", "awaiting_turn", "awaiting_child"):
            assert st.is_eligible_for_abandonment(kind), kind


# ---------- Bad transitions ----------

class TestInvalidTransitions:

    def test_done_plus_anything_raises(self):
        with pytest.raises(AssertionError):
            st.step(st.Done(), st.Start())

    def test_pending_with_user_replied_raises(self):
        with pytest.raises(AssertionError):
            st.step(_start_node(), st.UserReplied(reply="x"))


# ---------- End-to-end micro-trace ----------

class TestFullTrace:

    def test_call_then_return_round_trip(self):
        """Pending → Start → AwaitingTurn → TurnCompleted(Call) → AwaitingChild
           → ChildDone → AwaitingTurn → TurnCompleted(Return) → Done."""
        s = _start_node(parent="root")

        # 1. Start
        s, effs = st.step(s, st.Start())
        assert isinstance(s, st.AwaitingTurn)
        assert isinstance(effs[0], st.RunTurn) and effs[0].mode == "fork"

        # 2. Turn produces a CALL
        s, effs = st.step(s, st.TurnCompleted(envelope=Call(task="sub"),
                                              session_id="me-forked"))
        assert isinstance(s, st.AwaitingChild)
        spawn = effs[0]
        assert isinstance(spawn, st.SpawnChild)
        child_id = s.child_id

        # 3. Child returns
        s, effs = st.step(s, st.ChildDone(child_id=child_id, result="42"))
        assert isinstance(s, st.AwaitingTurn)
        assert isinstance(effs[0], st.RunTurn) and effs[0].mode == "resume"

        # 4. Self resumes and returns
        s, effs = st.step(s, st.TurnCompleted(envelope=Return(result="done"),
                                              session_id="me-forked"))
        assert isinstance(s, st.Done)
        assert s.result == "done"
        assert effs == []
