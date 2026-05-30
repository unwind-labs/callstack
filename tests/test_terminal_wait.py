"""Tests for `agent_callstack.terminal_wait`.

The wait helper is the safety net that gates `reporter.finalize` on
non-terminal nodes receiving a late `op:return` / `op:yield` envelope
on their child JSONL — see PRD `prd-don-t-seal-report-yaml-virtual-harp.md`.

These tests pin the four signals the helper must honor:

1. A late `op:return` envelope on the JSONL transitions the node to `Done`.
2. A late `op:yield` envelope on the JSONL transitions the node to `AwaitingUser`.
3. Budget exhaustion with no envelope transitions the node to `Timeout`.
4. The envelope MUST be parsed out of `message.content[*].text` fenced
   blocks (the exact shape Claude Code writes), NOT via raw substring
   match on the JSONL line.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import agent_callstack.state as st
import pytest
from agent_callstack.driver import Node, Tree
from agent_callstack.session import SessionRef
from agent_callstack.terminal_wait import wait_for_terminal_signals

# ---------- helpers ----------


def _make_node(*, clone_path: Path, session_id: str = "sess-x") -> Node:
    """Construct a Node already in AwaitingTurn — i.e. the shape it has
    when finalize would have otherwise sealed it as `running`."""
    nid = "abc123ef" + "0" * 24
    return Node(
        id=nid,
        task="do thing",
        state=st.AwaitingTurn(session_id=session_id),
        clone_path=str(clone_path),
    )


def _make_tree(node: Node, tmp_path: Path) -> Tree:
    parent_file = tmp_path / "parent.jsonl"
    parent_file.write_text("")
    return Tree(
        root_session=SessionRef(session_id="parent-sess", file=parent_file),
        nodes=[node],
        base_depth=0,
    )


def _assistant_envelope_line(
    env_text: str, *, session_id: str = "sess-x", timestamp: str = "2026-05-18T15:49:09.206Z"
) -> str:
    """One JSONL row in the shape Claude Code emits for assistant turns,
    with `env_text` (a fenced ```json envelope) embedded in the text
    content block — the same escaping the repro JSONL exhibits."""
    record = {
        "type": "assistant",
        "sessionId": session_id,
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": env_text},
            ],
        },
        "timestamp": timestamp,
    }
    return json.dumps(record)


def _session_header_line(timestamp: str) -> str:
    """The first row Claude Code writes for a (forked) session — a
    `queue-operation` header stamped with the fork-creation wall-clock.
    In a forked JSONL this precedes the replayed parent transcript, so the
    recovered-duration arithmetic reads *this* row's timestamp as the
    session start (not the older replayed-record timestamps)."""
    return json.dumps({"type": "queue-operation", "timestamp": timestamp})


def _fenced(op: str, **payload) -> str:
    return "```json\n" + json.dumps({"op": op, **payload}) + "\n```"


# ---------- tests ----------


class TestRecoverReturnEnvelope:
    def test_late_return_transitions_to_done(self, tmp_path):
        clone = tmp_path / "child.jsonl"
        clone.write_text("")
        node = _make_node(clone_path=clone)
        tree = _make_tree(node, tmp_path)

        envelope_text = _fenced("return", result="late-but-recovered", summary="went well", next="next-step")

        # The session JSONL opens with a header stamped at fork-creation
        # (the node's true start) and ends with the late return envelope.
        # 15:48:04.000 -> 15:49:09.206 = 65.206s of real work.
        session_start = "2026-05-18T15:48:04.000Z"
        envelope_ts = "2026-05-18T15:49:09.206Z"
        clone.write_text(_session_header_line(session_start) + "\n")

        # Append the envelope on a background thread ~150ms after wait
        # starts. The waiter must observe it within the 5s budget.
        def append_late():
            time.sleep(0.15)
            with clone.open("a") as fh:
                fh.write(_assistant_envelope_line(envelope_text, timestamp=envelope_ts) + "\n")

        threading.Thread(target=append_late, daemon=True).start()

        wait_for_terminal_signals(tree, wait_budget_seconds=5.0)

        assert isinstance(node.state, st.Done)
        assert node.state.result == "late-but-recovered"
        assert node.state.summary == "went well"
        assert node.state.suggested_next == "next-step"
        # Denormalized fields updated for the merged report.
        assert node.result == "late-but-recovered"
        assert node.summary == "went well"
        assert node.suggested_next == "next-step"
        # Duration is reconstructed from the JSONL's own timestamps
        # (envelope ts - session-start ts), NOT the finalize-wait latency
        # (~0.15s here). Pinning the exact value guards against a
        # regression back to measuring wait time.
        assert node.duration == pytest.approx(65.206, abs=0.01)

    def test_clock_skew_clamps_recovered_duration_to_zero(self, tmp_path):
        # If the envelope's timestamp PRECEDES the session-start header
        # (clock skew between the writer and the fork-creation stamp), the
        # raw `end - start` arithmetic goes negative. The recovery path
        # guards with `max(0.0, ...)` (terminal_wait.py) precisely so a
        # nonsensical negative duration never lands in the merged report.
        # Pinning the clamp guards against someone dropping the max().
        clone = tmp_path / "child.jsonl"
        node = _make_node(clone_path=clone)
        tree = _make_tree(node, tmp_path)

        envelope_text = _fenced("return", result="ok", summary="s", next="n")
        # session-start AFTER the envelope -> negative raw duration.
        session_start = "2026-05-18T15:49:09.206Z"
        envelope_ts = "2026-05-18T15:48:04.000Z"
        clone.write_text(_session_header_line(session_start) + "\n")

        def append_late():
            time.sleep(0.15)
            with clone.open("a") as fh:
                fh.write(_assistant_envelope_line(envelope_text, timestamp=envelope_ts) + "\n")

        threading.Thread(target=append_late, daemon=True).start()

        wait_for_terminal_signals(tree, wait_budget_seconds=5.0)

        assert isinstance(node.state, st.Done)
        # Clamped, never negative.
        assert node.duration == 0.0


class TestRecoverYieldEnvelope:
    def test_late_yield_transitions_to_awaiting_user(self, tmp_path):
        clone = tmp_path / "child.jsonl"
        clone.write_text("")
        node = _make_node(clone_path=clone)
        tree = _make_tree(node, tmp_path)

        envelope_text = _fenced("yield", question="Enter MFA code")

        def append_late():
            time.sleep(0.1)
            with clone.open("a") as fh:
                fh.write(_assistant_envelope_line(envelope_text) + "\n")

        threading.Thread(target=append_late, daemon=True).start()

        wait_for_terminal_signals(tree, wait_budget_seconds=5.0)

        assert isinstance(node.state, st.AwaitingUser)
        assert node.state.question == "Enter MFA code"
        # AwaitingUser is suspended, not terminal — but it IS what
        # `reporter.finalize` is supposed to seal as `yielded`.


class TestTimeoutOnBudgetExhaustion:
    def test_no_envelope_landing_yields_timeout(self, tmp_path):
        clone = tmp_path / "child.jsonl"
        clone.write_text("")  # never appended to
        node = _make_node(clone_path=clone)
        tree = _make_tree(node, tmp_path)

        wait_for_terminal_signals(tree, wait_budget_seconds=0.5)

        assert isinstance(node.state, st.Timeout)
        # Status label must surface the new `timeout` value so callers
        # can distinguish "we gave up waiting" from "child errored".
        assert node.status == "timeout"

    def test_missing_clone_path_still_times_out(self, tmp_path):
        # A node that never resolved its session JSONL — the waiter has
        # nothing to tail, so the only valid outcome is budget expiry.
        node = Node(
            id="x" * 32,
            task="t",
            state=st.AwaitingTurn(session_id="sess-y"),
            clone_path=None,
        )
        tree = _make_tree(node, tmp_path)

        wait_for_terminal_signals(tree, wait_budget_seconds=0.2)

        assert isinstance(node.state, st.Timeout)


class TestZeroBudgetIsNoOp:
    def test_zero_budget_skips_wait_entirely(self, tmp_path):
        """CALLSTACK_FINALIZE_WAIT_SECONDS=0 must preserve the pre-fix
        'seal immediately' behavior — used by tests that explicitly want
        to inspect the legacy report shape."""
        clone = tmp_path / "child.jsonl"
        clone.write_text("")
        node = _make_node(clone_path=clone)
        tree = _make_tree(node, tmp_path)

        wait_for_terminal_signals(tree, wait_budget_seconds=0.0)

        # State untouched — finalize will seal whatever's there.
        assert isinstance(node.state, st.AwaitingTurn)


class TestTerminalNodesAreSkipped:
    def test_done_node_left_alone(self, tmp_path):
        clone = tmp_path / "child.jsonl"
        clone.write_text("")
        node = _make_node(clone_path=clone)
        node.state = st.Done(session_id="sess-x", result="already done")
        tree = _make_tree(node, tmp_path)

        wait_for_terminal_signals(tree, wait_budget_seconds=5.0)

        # Returned immediately — Done is terminal.
        assert isinstance(node.state, st.Done)
        assert node.state.result == "already done"

    def test_failed_node_left_alone(self, tmp_path):
        clone = tmp_path / "child.jsonl"
        clone.write_text("")
        node = _make_node(clone_path=clone)
        node.state = st.Failed(error="boom", session_id="sess-x")
        tree = _make_tree(node, tmp_path)

        wait_for_terminal_signals(tree, wait_budget_seconds=5.0)

        assert isinstance(node.state, st.Failed)

    def test_awaiting_user_left_alone(self, tmp_path):
        clone = tmp_path / "child.jsonl"
        clone.write_text("")
        node = _make_node(clone_path=clone)
        node.state = st.AwaitingUser(session_id="sess-x", question="?")
        tree = _make_tree(node, tmp_path)

        wait_for_terminal_signals(tree, wait_budget_seconds=5.0)

        # AwaitingUser is parked waiting for the user — not the wait
        # helper's problem.
        assert isinstance(node.state, st.AwaitingUser)


class TestEnvelopeShapeIsTheReproShape:
    """The repro JSONL has the envelope inside `message.content[*].text`
    as a fenced ```json block. A naive substring match for `"op":` on
    the raw JSONL line WOULD find the escaped form `\\"op\\":` — but
    that's an accident of the JSON encoding, not a robust signal.
    This test pins that we go through `parse_envelope` on the
    decoded text, not a substring grep."""

    def test_envelope_inside_escaped_text_field_is_parsed(self, tmp_path):
        clone = tmp_path / "child.jsonl"
        clone.write_text("")
        node = _make_node(clone_path=clone)
        tree = _make_tree(node, tmp_path)

        # Build the exact shape the repro JSONL line 136 had: an assistant
        # message whose `content[0].text` is a long string containing a
        # fenced ```json envelope, all properly JSON-escaped on the wire.
        wire_line = json.dumps(
            {
                "type": "assistant",
                "sessionId": "sess-x",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Write permissions appear blocked. Returning to "
                                "parent.\n\n"
                                "```json\n"
                                '{"op": "return", "result": "phase 3 done", '
                                '"summary": "all good"}\n'
                                "```"
                            ),
                        },
                    ],
                },
            }
        )
        # Confirm the wire form is genuinely escaped (the on-disk
        # representation of `"op":"return"` is `\"op\": \"return\"`).
        assert '\\"op\\": \\"return\\"' in wire_line

        def append_late():
            time.sleep(0.1)
            with clone.open("a") as fh:
                fh.write(wire_line + "\n")

        threading.Thread(target=append_late, daemon=True).start()

        wait_for_terminal_signals(tree, wait_budget_seconds=3.0)

        assert isinstance(node.state, st.Done)
        assert node.state.result == "phase 3 done"
        assert node.state.summary == "all good"


class TestIgnoresIrrelevantJsonlRecords:
    def test_non_assistant_records_are_skipped(self, tmp_path):
        """User/system/queue-operation rows must not be parsed as
        envelopes even when they happen to contain `op:` strings."""
        clone = tmp_path / "child.jsonl"
        clone.write_text("")
        node = _make_node(clone_path=clone)
        tree = _make_tree(node, tmp_path)

        noise = json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": '```json\n{"op": "return", "result": "NOPE"}\n```'}],
                },
            }
        )

        def append_noise_then_real():
            time.sleep(0.1)
            with clone.open("a") as fh:
                fh.write(noise + "\n")
            time.sleep(0.1)
            with clone.open("a") as fh:
                fh.write(_assistant_envelope_line(_fenced("return", result="real")) + "\n")

        threading.Thread(target=append_noise_then_real, daemon=True).start()

        wait_for_terminal_signals(tree, wait_budget_seconds=3.0)

        assert isinstance(node.state, st.Done)
        assert node.state.result == "real"


class TestNestedNodesAreWaitedOnToo:
    def test_child_node_envelope_recovered(self, tmp_path):
        """The wait helper must traverse the whole tree, not just root
        nodes. Otherwise a stuck grandchild stays `running` forever."""
        child_clone = tmp_path / "child.jsonl"
        child_clone.write_text("")
        root_clone = tmp_path / "root.jsonl"
        root_clone.write_text("")

        child = _make_node(clone_path=child_clone, session_id="child-sess")
        root = _make_node(clone_path=root_clone, session_id="root-sess")
        # Mark root as already Done so only the child needs waiting.
        root.state = st.Done(session_id="root-sess", result="root-ok")
        root.children = [child]
        tree = _make_tree(root, tmp_path)

        def append_late():
            time.sleep(0.1)
            with child_clone.open("a") as fh:
                fh.write(
                    _assistant_envelope_line(_fenced("return", result="child-recovered"), session_id="child-sess")
                    + "\n"
                )

        threading.Thread(target=append_late, daemon=True).start()

        wait_for_terminal_signals(tree, wait_budget_seconds=3.0)

        assert isinstance(child.state, st.Done)
        assert child.state.result == "child-recovered"
        # Root state untouched.
        assert isinstance(root.state, st.Done)
        assert root.state.result == "root-ok"


@pytest.mark.parametrize(
    "applied_envelope, expected_state",
    [
        ("return", st.Done),
        ("yield", st.AwaitingUser),
    ],
)
def test_envelope_routed_through_state_machine(
    tmp_path,
    applied_envelope,
    expected_state,
):
    """Recovery goes through the canonical `state.step` transition, so a
    recovered `op:return` lands on `Done` and `op:yield` on `AwaitingUser`
    — exactly as the live driver would have applied them on `AwaitingTurn`.
    Only `AwaitingTurn` can absorb a `TurnCompleted`; an `AwaitingChild`
    node has no in-flight turn whose envelope this could be, so the live
    driver never fires `TurnCompleted` there and the waiter doesn't either
    (it falls through to the Timeout path)."""
    clone = tmp_path / "child.jsonl"
    clone.write_text("")
    node = _make_node(clone_path=clone)
    tree = _make_tree(node, tmp_path)

    envelope_text = _fenced(
        applied_envelope,
        **({"result": "x"} if applied_envelope == "return" else {"question": "?"}),
    )

    def append_late():
        time.sleep(0.1)
        with clone.open("a") as fh:
            fh.write(_assistant_envelope_line(envelope_text) + "\n")

    threading.Thread(target=append_late, daemon=True).start()

    wait_for_terminal_signals(tree, wait_budget_seconds=3.0)

    assert isinstance(node.state, expected_state)


def test_awaiting_child_falls_through_to_timeout(tmp_path):
    """An `AwaitingChild` node can't absorb a late envelope (the parent's
    own turn hasn't resumed), so even with a `return` envelope on its
    JSONL it must seal as `Timeout`, never `Done`."""
    clone = tmp_path / "child.jsonl"
    clone.write_text(_assistant_envelope_line(_fenced("return", result="x")) + "\n")
    node = _make_node(clone_path=clone)
    node.state = st.AwaitingChild(session_id="sess-x", child_id="c1")
    tree = _make_tree(node, tmp_path)

    wait_for_terminal_signals(tree, wait_budget_seconds=0.5)

    assert isinstance(node.state, st.Timeout)
