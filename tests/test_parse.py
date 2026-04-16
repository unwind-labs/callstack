"""Tests for parse_agent_output and _format_error."""

import json

from callstack import parse_agent_output, _format_error


class TestParseAgentOutput:
    """Tests for the three control markers: CALL, YIELD, RETURN."""

    # --- RETURN ---

    def test_return_extracts_result(self):
        output = "Some work done\n---RETURN---\nHere is the result"
        result = parse_agent_output(output)
        assert result["status"] == "complete"
        assert result["result"] == "Here is the result"

    def test_return_with_multiline_result(self):
        output = "---RETURN---\nLine 1\nLine 2\nLine 3"
        result = parse_agent_output(output)
        assert result["status"] == "complete"
        assert "Line 1\nLine 2\nLine 3" == result["result"]

    def test_return_with_no_text_after_marker(self):
        output = "---RETURN---"
        result = parse_agent_output(output)
        assert result["status"] == "complete"

    def test_return_with_only_whitespace_after_marker(self):
        output = "---RETURN---\n   \n  "
        result = parse_agent_output(output)
        assert result["status"] == "complete"

    # --- YIELD ---

    def test_yield_extracts_question(self):
        output = "---YIELD---\nWhat is your MFA code?"
        result = parse_agent_output(output)
        assert result["status"] == "yield"
        assert result["question"] == "What is your MFA code?"

    def test_yield_with_preamble(self):
        output = "I need more info.\n---YIELD---\nPlease confirm"
        result = parse_agent_output(output)
        assert result["status"] == "yield"
        assert result["question"] == "Please confirm"

    def test_yield_with_no_text_after_marker(self):
        output = "---YIELD---"
        result = parse_agent_output(output)
        assert result["status"] == "yield"
        assert result["question"] == ""

    # --- CALL ---

    def test_call_extracts_task(self):
        output = "---CALL---\nImplement auth module"
        result = parse_agent_output(output)
        assert result["status"] == "call"
        assert result["task"] == "Implement auth module"

    def test_call_with_preamble(self):
        output = "I'll delegate this.\n---CALL---\nWrite JWT middleware"
        result = parse_agent_output(output)
        assert result["status"] == "call"
        assert result["task"] == "Write JWT middleware"

    def test_call_with_no_text_after_marker(self):
        output = "---CALL---"
        result = parse_agent_output(output)
        assert result["status"] == "call"
        assert result["task"] == ""

    # --- No marker ---

    def test_no_marker_returns_complete(self):
        result = parse_agent_output("just regular output")
        assert result["status"] == "complete"
        assert "result" not in result

    def test_empty_string(self):
        result = parse_agent_output("")
        assert result["status"] == "complete"

    # --- Priority (CALL > YIELD > RETURN when multiple present) ---

    def test_call_takes_priority_over_return(self):
        output = "---CALL---\ntask\n---RETURN---\nresult"
        result = parse_agent_output(output)
        assert result["status"] == "call"

    def test_call_takes_priority_over_yield(self):
        output = "---CALL---\ntask\n---YIELD---\nquestion"
        result = parse_agent_output(output)
        assert result["status"] == "call"

    def test_yield_takes_priority_over_return(self):
        output = "---YIELD---\nquestion\n---RETURN---\nresult"
        result = parse_agent_output(output)
        assert result["status"] == "yield"


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
