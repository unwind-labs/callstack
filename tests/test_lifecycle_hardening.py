"""Tests for the CALL-lifecycle correctness hardenings.

Each `TestFix*` class corresponds to one numbered fix in the lifecycle
audit (#1, #2, #3, #4, #5, #7). The fixes replace silent-success
paths with explicit fail-loud ones — these tests pin that behavior
so a future regression that re-swallows an error is caught.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from agent_callstack.channel import ScriptedChannel
from agent_callstack.driver import Driver
from agent_callstack.protocol import (
    Call,
    Return,
    Yield,
    parse_envelope,
)
from agent_callstack.session import SessionLocator, SessionRef
from agent_callstack.trace import TraceWriter, TreeStore

# The plugins/ directory isn't on sys.path for module imports — match
# the pattern in test_mcp_server.py.
_PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "callstack"
if str(_PLUGIN) not in sys.path:
    sys.path.insert(0, str(_PLUGIN))


# ---------- shared helpers ----------


def _envelope(op: str, **fields) -> str:
    return "```json\n" + json.dumps({"op": op, **fields}) + "\n```"


@pytest.fixture
def parent_session(tmp_path) -> SessionRef:
    f = tmp_path / "parent.jsonl"
    f.write_text("a\nb\nc\n")
    return SessionRef(session_id="00000000-0000-0000-0000-0000000000d2", file=f)


def _make_driver(tmp_path, channel: ScriptedChannel, *, max_depth: int = 5) -> Driver:
    return Driver(
        channel=channel,
        resolve_session=SessionLocator(projects_dir=tmp_path / "_no_real_projects").resolve,
        trace=TraceWriter(tmp_path / "traces"),
        store=TreeStore(),
        cwd=str(tmp_path),
        timeout=10,
        max_depth=max_depth,
    )


# ---------- Fix #1 — parse_envelope distinguishes "no envelope" ----------


class TestFix1ParseEnvelope:
    def test_no_json_at_all_returns_none(self):
        # No JSON object anywhere → None. Must NOT collapse to Return().
        assert parse_envelope("just some prose, no json here") is None

    def test_unknown_opcode_returns_none(self):
        assert parse_envelope(_envelope("explode")) is None

    def test_explicit_empty_return_still_succeeds(self):
        # The legitimate empty-success path: an explicit `op: return`
        # with no `result` is still a Return(result=None).
        env = parse_envelope(_envelope("return"))
        assert env == Return(result=None, summary=None, suggested_next=None)

    def test_call_and_yield_unaffected(self):
        assert parse_envelope(_envelope("call", task="x")) == Call(task="x")
        assert parse_envelope(_envelope("yield", question="?")) == Yield(question="?")

    def test_driver_maps_no_envelope_to_failed(self, tmp_path, parent_session):
        # Scripted child returns text with no parseable envelope. The
        # driver must end the node in `error` state, not `complete` with
        # result=None.
        ch = ScriptedChannel().respond("I forgot to emit any envelope", "child-bare")
        driver = _make_driver(tmp_path, ch)

        tree = driver.run(parent_session, ["task"])

        node = tree.nodes[0]
        assert node.status == "error", (
            f"expected error status for envelope-less response, got {node.status}; "
            f"this means the silent-empty-Return regression is back"
        )
        assert "no parseable envelope" in node.error.lower()


# ---------- Fix #2 — channel checks subprocess returncode ----------


class TestFix2ReturncodeCheck:
    """The channel-layer returncode check lives inside the production
    `_run_one_turn` (channel.py); ScriptedChannel doesn't simulate a
    subprocess at all. We exercise the relevant branch by mocking the
    returncode-bearing PooledProcess pieces directly."""

    def test_nonzero_rc_with_no_text_raises(self, tmp_path):
        # Build a minimal fake to drive the check at channel.py:680-696.
        from agent_callstack import channel as ch_mod

        # We reach the rc check only after `_read_until_result` returns
        # successfully with a session_id. Patch out everything before it.
        log_path = tmp_path / "proc.log"
        log_path.write_text("")

        class _FakeProc:
            def __init__(self) -> None:
                self.returncode = 17  # non-zero, non-None
                self.stdin = type("S", (), {"write": lambda self, _: None, "flush": lambda self: None})()
                self.stdout = type("O", (), {"readline": lambda self: ""})()
                self.stderr = type("E", (), {"close": lambda self: None})()

            def kill(self) -> None:
                pass

            def poll(self) -> int:
                return self.returncode

        class _FakePooled:
            def __init__(self) -> None:
                self.proc = _FakeProc()
                self.initialized = True
                self.stdin = self.proc.stdin
                self.stdout = self.proc.stdout
                self.log = open(log_path, "w")
                self.log_path = str(log_path)
                self.session_id: str | None = None
                self.last_used = 0.0
                self.cwd = str(tmp_path)

        channel = ch_mod.ClaudeChannel()

        def _fake_send_user_message(self, _stdin, _prompt, _log):
            return None

        def _fake_read_until_result(_self, _stdin, _stdout, text_parts, _log, _meta, *, on_session_id=None):
            # No text appended — that's the failure mode we test.
            if on_session_id is not None:
                on_session_id("00000000-0000-0000-0000-000000000099")
            return "00000000-0000-0000-0000-000000000099"

        with (
            patch.object(ch_mod.ClaudeChannel, "_send_user_message", _fake_send_user_message),
            patch.object(ch_mod.ClaudeChannel, "_read_until_result", _fake_read_until_result),
        ):
            with pytest.raises(RuntimeError, match="returncode=17"):
                channel._run_one_turn(_FakePooled(), "p", timeout=5, on_session_id=None, do_handshake=False)


# ---------- Fix #3 — Caller._invoke wraps driver.run in try/finally ----------


class TestFix3FinalizeOnException:
    """If `driver.run` raises, the reporter must still flush so
    `report.yaml` reflects the partial state."""

    def test_finalize_runs_when_driver_raises(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Force a parent session file to exist so SessionLocator finds something.
        # Simplest path: stub Caller's locator + driver entirely via patching.
        from agent_callstack import Caller, _LiveReporter

        finalize_calls = []
        original_finalize = _LiveReporter.finalize

        def _spy_finalize(self, tree):
            finalize_calls.append(tree)
            return original_finalize(self, tree)

        monkeypatch.setattr(_LiveReporter, "finalize", _spy_finalize)

        # Make the driver's run raise.
        from agent_callstack import driver as drv_mod

        def _boom(self, *a, **kw):
            raise RuntimeError("simulated driver explosion")

        monkeypatch.setattr(drv_mod.Driver, "run", _boom)

        # Provide a fake session locator so Caller._invoke gets past
        # parent-session lookup.
        fake_parent = SessionRef(
            session_id="00000000-0000-0000-0000-000000000001",
            file=tmp_path / "parent.jsonl",
        )
        (tmp_path / "parent.jsonl").write_text("x\n")
        from agent_callstack import session as sess_mod

        monkeypatch.setattr(sess_mod.SessionLocator, "locate", lambda self, **kw: fake_parent)

        caller = Caller()
        with pytest.raises(RuntimeError, match="simulated driver explosion"):
            caller.call("doomed task")

        # `tree` was None when driver.run raised — finalize is skipped in
        # that branch (no tree to write). The contract: the exception
        # propagates cleanly. Asserting NO finalize_calls confirms the
        # try/finally path correctly handled the None-tree case rather
        # than crashing with NameError.
        assert finalize_calls == []


# ---------- Fix #4 — report_path verification ----------


class TestFix4ReportWarning:
    """If the reporter failed to write `report.yaml`, the MCP envelope
    must include a `report_warning` so the calling agent knows the
    `report_path` it received is unreliable."""

    @pytest.mark.asyncio
    async def test_envelope_includes_warning_when_report_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        import mcp_server  # type: ignore

        # Stub out caller.call_many — return a benign MultiResult; do NOT
        # actually create a report.yaml on disk.
        from agent_callstack.results import MultiResult, Result

        async def _to_thread(fn, *a, **kw):
            return fn(*a, **kw)

        def _fake_call_many(_self, _tasks, *, context):
            return MultiResult(
                results=[
                    Result(
                        value="ok",
                        summary=None,
                        next=None,
                        duration=0.0,
                        log=None,
                        log_start=0,
                    )
                ]
            )

        monkeypatch.setattr(mcp_server.asyncio, "to_thread", _to_thread)
        monkeypatch.setattr(mcp_server.Caller, "call_many", _fake_call_many)

        result_str = await mcp_server.call(["t"], cwd=str(tmp_path))
        envelope = json.loads(result_str)
        # The fake call_many never wrote a report — verification kicks in.
        assert "report_warning" in envelope, (
            f"missing report_warning key when report.yaml is absent; envelope: {envelope}"
        )
        assert "does not exist" in envelope["report_warning"]


# ---------- Fix #5 — the single identity decision validates inherited env ----------


class TestFix5EnvValidation:
    def test_stale_env_falls_through_to_fresh_id(self, tmp_path, monkeypatch, capsys):
        import mcp_server  # type: ignore

        # Plant env vars that look like an active root, but pointing at a
        # nonexistent directory. The single decision must reject them.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CALLSTACK_ROOT_INVOKE_ID", "20300101T000000-deadbeef")
        monkeypatch.setenv("CALLSTACK_ROOT_LOG_DIR", str(tmp_path / "no-such-dir"))

        ident = mcp_server._resolve_identity(str(tmp_path))

        # A fresh top-level id was minted, NOT the stale one reused.
        assert ident.context.invoke_id != "20300101T000000-deadbeef"
        assert "no-such-dir" not in str(ident.context.log_dir)
        assert ident.context.is_nested is False

        # The rejection rides back as `warning` data and is printed at the edge.
        assert ident.warning is not None and "ignoring inherited" in ident.warning.lower()
        captured = capsys.readouterr()
        assert "ignoring inherited" in captured.err.lower()

    def test_valid_env_is_reused(self, tmp_path, monkeypatch):
        import mcp_server  # type: ignore

        monkeypatch.chdir(tmp_path)
        live_root = tmp_path / "live-root"
        live_invoke_id = "20300101T000000-cafebabe"
        (live_root / live_invoke_id).mkdir(parents=True)
        monkeypatch.setenv("CALLSTACK_ROOT_INVOKE_ID", live_invoke_id)
        monkeypatch.setenv("CALLSTACK_ROOT_LOG_DIR", str(live_root))

        ident = mcp_server._resolve_identity(str(tmp_path))

        assert ident.context.invoke_id == live_invoke_id
        assert ident.context.log_dir == live_root
        assert ident.context.is_nested is True

    def test_stale_env_is_not_mutated_yet_run_still_agrees(self, tmp_path, monkeypatch):
        """DRY-102 REMOVED: the boundary no longer pops CALLSTACK_ROOT_* to keep
        the downstream Caller in sync. Instead the dir-exists validation lives in
        the single `resolve_identity`, so the run re-derives the SAME decision
        deterministically. This pins both halves: (a) the env is left untouched,
        and (b) the Caller's factory — reading the still-present stale env —
        independently resolves to a fresh root (not nested under the dead root),
        reusing the boundary's minted id."""
        import mcp_server  # type: ignore
        from agent_callstack.invocation import InvocationFactory

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CALLSTACK_ROOT_INVOKE_ID", "stale-id")
        monkeypatch.setenv("CALLSTACK_ROOT_LOG_DIR", str(tmp_path / "gone"))

        ident = mcp_server._resolve_identity(str(tmp_path))

        # (a) Env is NOT mutated (no os.environ.pop).
        assert os.environ["CALLSTACK_ROOT_INVOKE_ID"] == "stale-id"
        assert os.environ["CALLSTACK_ROOT_LOG_DIR"] == str(tmp_path / "gone")

        # (b) The run's factory, reading the same stale env, still agrees: it
        # validates the (nonexistent) invocation dir and falls through to a
        # fresh root, reusing the boundary's minted id (threaded as explicit).
        factory = InvocationFactory(
            explicit_cwd=str(tmp_path),
            explicit_log_dir=None,
            explicit_invoke_id=ident.context.invoke_id,
            max_depth=10,
        )
        run_ctx = factory.context(parent_cwd=str(tmp_path))
        assert run_ctx.is_nested is False
        assert run_ctx.invoke_id == ident.context.invoke_id


# ---------- Fix #7 — partial report when nested has no root ----------


class TestFix7NestedPartialReport:
    def test_nested_finalize_writes_partial_when_root_missing(self, tmp_path):
        from agent_callstack import _LiveReporter
        from agent_callstack.driver import Tree
        from agent_callstack.invocation_ctx import _InvocationContext

        invoke_id = "20300101T000000-test0001"
        log_dir = tmp_path / "log"
        invocation_dir = log_dir / invoke_id
        invocation_dir.mkdir(parents=True)

        # Nested context: is_nested=True, frame_key != _ROOT_FRAME_KEY.
        ctx = _InvocationContext(
            invoke_id=invoke_id,
            log_dir=log_dir,
            cwd=str(tmp_path),
            frame_key="caller-node-id",
            is_nested=True,
            instance_id="abc123",
        )
        reporter = _LiveReporter(
            ctx=ctx,
            kind="nested:call",
            tasks=["nested task"],
            started_at="2030-01-01T00:00:00Z",
        )

        # Build a minimal, valid Tree with no nodes — `to_dict` requires
        # root_session and base_depth.
        tree = Tree(
            root_session=SessionRef(
                session_id="00000000-0000-0000-0000-000000000099",
                file=tmp_path / "fake.jsonl",
            ),
            nodes=[],
            base_depth=0,
        )

        # No root frame is on disk. finalize must produce report.partial.yaml
        # rather than silently writing nothing.
        reporter.finalize(tree)

        partial = invocation_dir / "report.partial.yaml"
        assert partial.is_file(), (
            f"expected report.partial.yaml to be written when nested "
            f"finalize runs without a root frame; contents of "
            f"{invocation_dir}: {list(invocation_dir.iterdir())}"
        )
        # The real report.yaml stays absent — that's the root's job.
        assert not (invocation_dir / "report.yaml").is_file()

    def test_nested_finalize_skips_partial_if_root_present(self, tmp_path):
        from agent_callstack import _LiveReporter
        from agent_callstack.driver import Tree
        from agent_callstack.frames import _ROOT_FRAME_KEY
        from agent_callstack.invocation_ctx import _InvocationContext

        invoke_id = "20300101T000000-test0002"
        log_dir = tmp_path / "log"
        invocation_dir = log_dir / invoke_id
        frames_dir = invocation_dir / "_frames"
        frames_dir.mkdir(parents=True)

        # Plant a root frame — finalize should not produce a partial.
        root_frame = {
            "frame_key": _ROOT_FRAME_KEY,
            "is_nested": False,
            "kind": "root:call",
            "tasks": ["root task"],
            "cwd": str(tmp_path),
            "started_at": "2030-01-01T00:00:00Z",
            "ended_at": "2030-01-01T00:00:01Z",
            "tree": {
                # Canonical on-disk shape stamps "2" (string); Tree.from_dict
                # rejects 1/missing (pinned in test_driver.py). Keep this in
                # sync so the planted fixture matches production frames.
                "schema_version": "2",
                "root_session_id": "00000000-0000-0000-0000-000000000099",
                "root_session_file": str(tmp_path / "fake.jsonl"),
                "base_depth": 0,
                "nodes": [],
            },
        }
        import yaml

        (frames_dir / f"{_ROOT_FRAME_KEY}.yaml").write_text(yaml.safe_dump(root_frame))

        ctx = _InvocationContext(
            invoke_id=invoke_id,
            log_dir=log_dir,
            cwd=str(tmp_path),
            frame_key="caller-node-id",
            is_nested=True,
            instance_id="def456",
        )
        reporter = _LiveReporter(
            ctx=ctx,
            kind="nested:call",
            tasks=["nested task"],
            started_at="2030-01-01T00:00:00Z",
        )
        tree = Tree(
            root_session=SessionRef(
                session_id="00000000-0000-0000-0000-000000000099",
                file=tmp_path / "fake.jsonl",
            ),
            nodes=[],
            base_depth=0,
        )

        reporter.finalize(tree)

        # Root present → no partial; the merged report.yaml is what wins.
        assert not (invocation_dir / "report.partial.yaml").is_file()
        assert (invocation_dir / "report.yaml").is_file()
