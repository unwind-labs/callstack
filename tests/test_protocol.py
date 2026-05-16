"""Tests for protocol.parse_envelope: the agent-to-runtime control grammar."""
from __future__ import annotations

import json

from agent_callstack.protocol import (
    Call, Yield, Return, parse_envelope,
    starting_prompt, child_returned_prompt, SYSTEM_INSTRUCTION,
)


def _fenced(obj: dict) -> str:
    return "```json\n" + json.dumps(obj) + "\n```"


class TestParseEnvelope:

    # ---- Return ----

    def test_return_with_payload(self):
        env = parse_envelope(_fenced({"op": "return", "result": "hi",
                                      "summary": "did stuff", "next": "go"}))
        assert env == Return(result="hi", summary="did stuff", suggested_next="go")

    def test_return_empty(self):
        env = parse_envelope(_fenced({"op": "return"}))
        assert env == Return(result=None, summary=None, suggested_next=None)

    def test_return_only_result(self):
        env = parse_envelope(_fenced({"op": "return", "result": "ok"}))
        assert env == Return(result="ok")

    # ---- Yield ----

    def test_yield(self):
        env = parse_envelope(_fenced({"op": "yield", "question": "code?"}))
        assert env == Yield(question="code?")

    def test_yield_no_question(self):
        env = parse_envelope(_fenced({"op": "yield"}))
        assert env == Yield(question="")

    # ---- Call ----

    def test_call(self):
        env = parse_envelope(_fenced({"op": "call", "task": "do thing"}))
        assert env == Call(task="do thing")

    # ---- Edge cases ----

    def test_no_envelope_returns_none(self):
        # No JSON object → None (driver maps this to TurnFailed). An
        # explicit `{"op":"return"}` is still a legitimate empty Return —
        # see test_return_empty above.
        assert parse_envelope("nothing here") is None

    def test_unknown_op_returns_none(self):
        # Unknown opcode is a protocol violation, not a successful empty
        # return — surface as None so the driver fails the turn loudly.
        assert parse_envelope(_fenced({"op": "explode"})) is None

    def test_last_envelope_wins(self):
        text = (
            "first thoughts " + _fenced({"op": "call", "task": "ignored"})
            + "\nfinal: " + _fenced({"op": "return", "result": "winner"})
        )
        assert parse_envelope(text) == Return(result="winner")

    def test_malformed_fenced_falls_through(self):
        # The fenced block fails to JSON-parse and there's no other JSON
        # object anywhere in the text → None.
        assert parse_envelope("```json\n{not json}\n```") is None

    def test_braces_in_strings_handled(self):
        text = _fenced({"op": "return", "result": "has {braces} inside"})
        env = parse_envelope(text)
        assert isinstance(env, Return) and env.result == "has {braces} inside"

    def test_bare_json_without_fence(self):
        env = parse_envelope('result: {"op": "return", "result": "bare"}')
        assert env == Return(result="bare")


class TestPromptHelpers:

    def test_starting_prompt_includes_system_instruction(self):
        p = starting_prompt("do X", task_id="abc12345")
        assert SYSTEM_INSTRUCTION in p
        assert "## Starting Task [abc12345]" in p
        assert p.endswith("Task: do X")

    def test_starting_prompt_no_task_id(self):
        p = starting_prompt("just do it")
        assert "## Starting Task\n\n" in p
        assert "[" not in p.split("Starting Task")[1].split("\n")[0]

    def test_starting_prompt_is_compact(self):
        # Guardrail: the per-fork preamble is duplicated on every nested
        # fork (inherited via --fork-session), so it must stay small.
        # Bumping this past 1000 chars should be a deliberate decision.
        p = starting_prompt("x", task_id="abc12345")
        assert len(p) < 1000, f"starting_prompt grew to {len(p)} chars"

    def test_child_returned_string(self):
        assert child_returned_prompt("hello") == \
            "Your child completed. Here is the result:\n\nhello"

    def test_child_returned_dict(self):
        msg = child_returned_prompt({"k": 1})
        assert msg.endswith('{"k": 1}')

    def test_child_returned_none(self):
        msg = child_returned_prompt(None)
        assert msg.endswith("\n\n")
