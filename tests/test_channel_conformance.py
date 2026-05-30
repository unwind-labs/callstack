"""Conformance tests for the `Channel.on_session_id` timing contract, plus
isolated unit tests of `TurnExecutor`.

Production `ClaudeChannel` fires `on_session_id` mid-turn (from its NDJSON
reader loop, on the `system init` message) — *before* `run_turn` returns. Any
test double must honor the same ordering, or the mid-turn session-id
propagation in `TurnExecutor` is never exercised faithfully (the gap RFC #1
flagged: `ScriptedChannel` used to be the only double, and the early-session +
`resolve_session->None` paths were unreachable from the suite).

These tests pin the contract for every in-process channel double and unit-test
`TurnExecutor` with no Driver/Tree involved — the deepening payoff of Phase 1.2.
A production arm against a real `claude` binary is intentionally out of scope
(gated); a recorded-NDJSON replay double would be the way to add it.
"""

from __future__ import annotations

from typing import Optional

import agent_callstack.state as st
import pytest
from agent_callstack.channel import ScriptedChannel, TurnResult, TurnTimeout, _fire_on_session_id
from agent_callstack.driver import TurnExecutor


def _return_envelope(result: str = "ok") -> str:
    return '```json\n{"op": "return", "result": "%s"}\n```' % result


# ---------------------------------------------------------------------------
# Channel doubles
# ---------------------------------------------------------------------------


class MidTurnSessionChannel:
    """A `Channel` that fires `on_session_id` MID-turn — before it builds and
    returns the `TurnResult` — matching `ClaudeChannel`'s reader-loop timing.
    Lets a test observe node/executor state at the exact moment the id lands."""

    def __init__(self, *, text: str, session_id: str):
        self.text = text
        self.session_id = session_id

    def run_turn(
        self,
        source_session_id: str,
        prompt: str,
        *,
        mode: str,
        cwd: Optional[str] = None,
        timeout: int = 300,
        extra_env: Optional[dict] = None,
        on_session_id=None,
        preallocated_session_id: Optional[str] = None,
    ) -> TurnResult:
        if on_session_id is not None and self.session_id:
            _fire_on_session_id(on_session_id, self.session_id)  # <-- mid-turn, before result
        return TurnResult(
            text=self.text,
            session_id=self.session_id,
            duration=0.0,
            api_request_id="",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            total_cost_usd=0.0,
        )


class _RaisingChannel:
    """Raises the given exception from `run_turn` (timeout or generic)."""

    def __init__(self, exc: Exception):
        self.exc = exc

    def run_turn(self, *a, on_session_id=None, **k) -> TurnResult:
        raise self.exc


class _RecordingTrace:
    def __init__(self):
        self.writes: list[dict] = []

    def write(self, **kw):
        self.writes.append(kw)


class _OrderRecorder:
    """Records whether `on_session_id` fired before `run_turn` returned, and
    how many times."""

    def __init__(self):
        self.fire_count = 0
        self.fired_before_return: Optional[bool] = None
        self._returned = False

    def on_session_id(self, sid: str) -> None:
        self.fire_count += 1
        if self.fired_before_return is None:
            self.fired_before_return = not self._returned

    def mark_returned(self) -> None:
        self._returned = True


# ---------------------------------------------------------------------------
# Conformance: on_session_id fires exactly once, before run_turn returns
# ---------------------------------------------------------------------------


def _conformant_channels():
    return [
        pytest.param(ScriptedChannel().respond(_return_envelope(), "new-sess"), id="scripted"),
        pytest.param(MidTurnSessionChannel(text=_return_envelope(), session_id="new-sess"), id="mid-turn"),
    ]


@pytest.mark.parametrize("channel", _conformant_channels())
def test_on_session_id_fires_once_before_return(channel):
    rec = _OrderRecorder()
    channel.run_turn("parent", "task", mode="fork", on_session_id=rec.on_session_id)
    rec.mark_returned()
    assert rec.fire_count == 1, "on_session_id must fire exactly once"
    assert rec.fired_before_return is True, "on_session_id must fire BEFORE run_turn returns (timing contract)"


# ---------------------------------------------------------------------------
# TurnExecutor in isolation — no Driver, no Tree
# ---------------------------------------------------------------------------


def _executor(channel, *, resolve_session=lambda sid, cwd: None) -> TurnExecutor:
    return TurnExecutor(
        channel=channel,
        trace=_RecordingTrace(),
        resolve_session=resolve_session,
        seed=None,
        cwd=None,
        timeout=300,
    )


def _run(executor, *, mode="fork", on_session_id=lambda sid: None):
    return executor.execute(
        st.RunTurn(source_session_id="p", prompt="t", mode=mode),
        depth=1,
        task="t",
        frame_key="node-abc",
        prior_duration=0.0,
        live_session_id=lambda: None,
        on_session_id=on_session_id,
    )


def test_early_session_callback_fires_during_a_fork_turn():
    # The previously-untestable path: a mid-turn id lands and the executor's
    # on_session_id callback runs while the turn is still in flight.
    seen: list[str] = []
    ex = _executor(MidTurnSessionChannel(text=_return_envelope("done"), session_id="sid-1"))
    outcome = _run(ex, on_session_id=lambda sid: seen.append(sid))
    assert seen == ["sid-1"]
    assert isinstance(outcome.event, st.TurnCompleted)


def test_resolve_session_none_leaves_clone_path_unset():
    # resolve_session returning None (session file not discoverable) is a
    # first-class outcome: clone_path stays None, the turn still completes.
    ex = _executor(
        MidTurnSessionChannel(text=_return_envelope(), session_id="sid-2"),
        resolve_session=lambda sid, cwd: None,
    )
    outcome = _run(ex)
    assert outcome.clone_path is None
    assert isinstance(outcome.event, st.TurnCompleted)


def test_resolve_session_hit_sets_clone_path():
    from pathlib import Path

    ex = _executor(
        MidTurnSessionChannel(text=_return_envelope(), session_id="sid-3"),
        resolve_session=lambda sid, cwd: Path("/tmp") / f"{sid}.jsonl",
    )
    outcome = _run(ex)
    assert outcome.clone_path == "/tmp/sid-3.jsonl"


def test_resume_mode_does_not_resolve_or_preallocate():
    # resume continues an existing session: no new id, so no clone-path resolve
    # and no mid-turn callback wiring.
    called = {"resolve": 0}

    def _resolve(sid, cwd):
        called["resolve"] += 1
        return None

    ex = _executor(MidTurnSessionChannel(text=_return_envelope(), session_id="sid-x"), resolve_session=_resolve)
    outcome = _run(ex, mode="resume")
    assert called["resolve"] == 0
    assert outcome.clone_path is None
    assert isinstance(outcome.event, st.TurnCompleted)


def test_turn_timeout_becomes_turnfailed_with_partial():
    ex = _executor(_RaisingChannel(TurnTimeout("timed out", partial="half a thought")))
    outcome = _run(ex)
    assert isinstance(outcome.event, st.TurnFailed)
    assert outcome.event.partial == "half a thought"
    assert "timed out" in outcome.event.error


def test_generic_exception_becomes_invocation_failed():
    ex = _executor(_RaisingChannel(RuntimeError("boom")))
    outcome = _run(ex)
    assert isinstance(outcome.event, st.TurnFailed)
    assert "Invocation failed: boom" == outcome.event.error


def test_unparseable_text_becomes_no_envelope_failure():
    ex = _executor(MidTurnSessionChannel(text="just some prose, no fenced json", session_id="sid-9"))
    outcome = _run(ex)
    assert isinstance(outcome.event, st.TurnFailed)
    assert "no parseable envelope" in outcome.event.error


def test_upstream_rate_limit_is_classified():
    text = "API Error: Server is temporarily limiting requests"
    ex = _executor(MidTurnSessionChannel(text=text, session_id="sid-10"))
    outcome = _run(ex)
    assert isinstance(outcome.event, st.TurnFailed)
    assert outcome.event.error.startswith("upstream_rate_limited:")
