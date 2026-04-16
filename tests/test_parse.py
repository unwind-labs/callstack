"""Tests for parse_agent_output and _format_error."""

import json

from callstack import parse_agent_output, _format_error


def _fenced(obj: dict) -> str:
    return "```json\n" + json.dumps(obj) + "\n```"


class TestParseAgentOutput:
    """Tests for the JSON envelope: op = call | yield | return."""

    # --- RETURN ---

    def test_return_extracts_result(self):
        output = "Some work done\n" + _fenced({"op": "return", "result": "Here is the result"})
        result = parse_agent_output(output)
        assert result["status"] == "complete"
        assert result["result"] == "Here is the result"

    def test_return_with_multiline_result(self):
        output = _fenced({"op": "return", "result": "Line 1\nLine 2\nLine 3"})
        result = parse_agent_output(output)
        assert result["status"] == "complete"
        assert result["result"] == "Line 1\nLine 2\nLine 3"

    def test_return_with_no_payload(self):
        output = _fenced({"op": "return"})
        result = parse_agent_output(output)
        assert result["status"] == "complete"
        assert result["result"] is None
        assert result["summary"] is None
        assert result["suggested_next"] is None

    # --- RETURN with summary and next ---

    def test_return_with_summary_and_next(self):
        output = _fenced({
            "op": "return",
            "result": "Created auth module",
            "summary": "Touched auth.py and middleware.py. Chose JWT over session cookies.",
            "next": "Run the test suite",
        })
        result = parse_agent_output(output)
        assert result["status"] == "complete"
        assert result["result"] == "Created auth module"
        assert result["summary"] == "Touched auth.py and middleware.py. Chose JWT over session cookies."
        assert result["suggested_next"] == "Run the test suite"

    def test_return_without_summary_or_next(self):
        output = _fenced({"op": "return", "result": "Just a plain result"})
        result = parse_agent_output(output)
        assert result["status"] == "complete"
        assert result["result"] == "Just a plain result"
        assert result["summary"] is None
        assert result["suggested_next"] is None

    def test_return_with_only_summary(self):
        output = _fenced({
            "op": "return",
            "result": "OK",
            "summary": "Nothing surprising",
        })
        result = parse_agent_output(output)
        assert result["summary"] == "Nothing surprising"
        assert result["suggested_next"] is None

    # --- YIELD ---

    def test_yield_extracts_question(self):
        output = _fenced({"op": "yield", "question": "What is your MFA code?"})
        result = parse_agent_output(output)
        assert result["status"] == "yield"
        assert result["question"] == "What is your MFA code?"

    def test_yield_with_preamble(self):
        output = "I need more info.\n" + _fenced({"op": "yield", "question": "Please confirm"})
        result = parse_agent_output(output)
        assert result["status"] == "yield"
        assert result["question"] == "Please confirm"

    def test_yield_with_no_question(self):
        output = _fenced({"op": "yield"})
        result = parse_agent_output(output)
        assert result["status"] == "yield"
        assert result["question"] == ""

    # --- CALL ---

    def test_call_extracts_task(self):
        output = _fenced({"op": "call", "task": "Implement auth module"})
        result = parse_agent_output(output)
        assert result["status"] == "call"
        assert result["task"] == "Implement auth module"

    def test_call_with_preamble(self):
        output = "I'll delegate this.\n" + _fenced({"op": "call", "task": "Write JWT middleware"})
        result = parse_agent_output(output)
        assert result["status"] == "call"
        assert result["task"] == "Write JWT middleware"

    def test_call_with_no_task(self):
        output = _fenced({"op": "call"})
        result = parse_agent_output(output)
        assert result["status"] == "call"
        assert result["task"] == ""

    # --- No envelope / malformed ---

    def test_no_envelope_returns_complete(self):
        result = parse_agent_output("just regular output with no JSON")
        assert result["status"] == "complete"
        assert "result" not in result

    def test_empty_string(self):
        result = parse_agent_output("")
        assert result["status"] == "complete"

    def test_malformed_fenced_json_falls_through(self):
        output = "```json\n{not valid json}\n```"
        result = parse_agent_output(output)
        # Malformed JSON → no envelope found → complete with no fields
        assert result["status"] == "complete"

    def test_unknown_op_returns_complete(self):
        output = _fenced({"op": "unknown_op", "data": "ignored"})
        result = parse_agent_output(output)
        assert result["status"] == "complete"
        assert "result" not in result

    # --- Last-envelope-wins ---

    def test_last_fenced_block_wins(self):
        """Earlier JSON in the response is ignored — only the last envelope parses."""
        output = (
            "While thinking, I considered " + _fenced({"op": "call", "task": "first idea"})
            + "\nbut actually...\n"
            + _fenced({"op": "return", "result": "final answer"})
        )
        result = parse_agent_output(output)
        assert result["status"] == "complete"
        assert result["result"] == "final answer"

    def test_preamble_with_unrelated_braces_ignored(self):
        """Stray JSON-looking content in prose should not hijack parsing when a valid fenced block exists."""
        output = (
            "Here's some example: {invalid jsonish}.\n"
            + _fenced({"op": "return", "result": "the real result"})
        )
        result = parse_agent_output(output)
        assert result["status"] == "complete"
        assert result["result"] == "the real result"

    def test_raw_json_without_fence_still_parses(self):
        """Fallback path: balanced {...} that parses is accepted even without fences."""
        output = 'Here is the outcome: {"op": "return", "result": "bare"}'
        result = parse_agent_output(output)
        assert result["status"] == "complete"
        assert result["result"] == "bare"


class TestFormatError:

    def test_basic_error(self):
        result = json.loads(_format_error("something broke"))
        assert result["error"] == "something broke"
        assert result["partial_result"] is None
        assert result["context"] is None

    def test_with_partial_output(self):
        result = json.loads(_format_error("timeout", partial_output="partial data"))
        assert result["error"] == "timeout"
        assert result["partial_result"] == "partial data"

    def test_with_context(self):
        result = json.loads(_format_error("fail", context="task was: foo"))
        assert result["context"] == "task was: foo"

    def test_all_fields(self):
        result = json.loads(_format_error("err", partial_output="partial", context="ctx"))
        assert result["error"] == "err"
        assert result["partial_result"] == "partial"
        assert result["context"] == "ctx"
