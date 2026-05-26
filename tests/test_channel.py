"""Tests for ClaudeChannel: CLI flag construction (_build_cmd), the
permission control-request handler (_answer_control_request, incl. the SEC-011
fail-closed deny), and the NDJSON reader loop teardown branches."""
from __future__ import annotations

import io
import json
import subprocess
import time

import pytest

import agent_callstack.channel as ch_mod
from agent_callstack.channel import (
    ClaudeChannel, ClaudePool, TurnTimeout, _NDJSON_MAX_LINE, _PooledProcess,
    _fire_on_session_id, _get_pool, _process_log_path, allow_all, shutdown_pool,
)


@pytest.fixture
def channel() -> ClaudeChannel:
    return ClaudeChannel(model="opus")


class TestBuildCmd:
    def test_fork_uses_resume_and_fork_session(self, channel):
        cmd = channel._build_cmd("parent-sid", "fork")
        assert "--resume" in cmd
        assert "parent-sid" in cmd
        assert "--fork-session" in cmd

    def test_fresh_omits_resume_and_fork_session(self, channel):
        cmd = channel._build_cmd("ignored", "fresh")
        assert "--resume" not in cmd
        assert "--fork-session" not in cmd
        # Still has the core invariants.
        assert "claude" in cmd[0]
        assert "stream-json" in cmd

    def test_resume_uses_resume_only(self, channel):
        cmd = channel._build_cmd("sid-123", "resume")
        assert "--resume" in cmd
        assert "sid-123" in cmd
        assert "--fork-session" not in cmd

    def test_model_flag_propagates(self, channel):
        cmd = channel._build_cmd("p", "fork")
        i = cmd.index("--model")
        assert cmd[i + 1] == "opus"

    def test_no_model_means_no_model_flag(self):
        cmd = ClaudeChannel()._build_cmd("p", "fresh")
        assert "--model" not in cmd

    def test_preallocated_uuid_pins_session_id(self, channel):
        """A pre-allocated UUID must be passed via --session-id on fork/fresh so
        the child's MCP server reads its own id from env, not by mtime guessing."""
        uuid = "00000000-0000-0000-0000-0000000000ab"
        cmd = channel._build_cmd("p", "fork", preallocated_session_id=uuid)
        i = cmd.index("--session-id")
        assert cmd[i + 1] == uuid

    def test_non_uuid_preallocation_is_ignored(self, channel):
        """A non-UUID preallocation must NOT reach the argv — it would be an
        invalid --session-id and is a caller bug, not something to forward."""
        cmd = channel._build_cmd("p", "fresh", preallocated_session_id="not-a-uuid")
        assert "--session-id" not in cmd


class TestFireOnSessionId:
    """R-M1: ClaudeChannel and ScriptedChannel share one helper for invoking
    the advisory on_session_id callback so the exception-handling can't drift.
    The helper logs a raise to stderr, plus the per-turn log when given, and
    never propagates into the turn (SEC-011)."""

    def test_success_does_not_log(self, capsys):
        log = io.StringIO()
        seen: list[str] = []
        _fire_on_session_id(seen.append, "sid-1", log)
        assert seen == ["sid-1"]
        assert log.getvalue() == ""
        assert capsys.readouterr().err == ""

    def test_raise_surfaces_to_both_stderr_and_log(self, capsys):
        log = io.StringIO()

        def boom(_sid: str) -> None:
            raise ValueError("nope")

        # Must not propagate — advisory observer.
        _fire_on_session_id(boom, "sid-1", log)
        logged = log.getvalue()
        err = capsys.readouterr().err
        assert "ValueError" in logged and "nope" in logged
        assert "ValueError" in err and "nope" in err

    def test_raise_without_log_still_hits_stderr(self, capsys):
        def boom(_sid: str) -> None:
            raise RuntimeError("kaboom")

        # ScriptedChannel passes no log sink — stderr only, no crash.
        _fire_on_session_id(boom, "sid-1")
        assert "RuntimeError" in capsys.readouterr().err


class TestConstructorValidation:
    """SEC-013: permission_mode is validated at construction so a typo or a
    hostile caller can't smuggle an arbitrary value into the `claude` argv."""

    def test_invalid_permission_mode_rejected(self):
        with pytest.raises(ValueError, match="invalid permission_mode"):
            ClaudeChannel(permission_mode="yolo")

    def test_known_permission_modes_accepted(self):
        # A representative valid mode constructs without error.
        assert ClaudeChannel(permission_mode="default") is not None


class TestAnswerControlRequest:
    """_answer_control_request turns a `can_use_tool` control_request into a
    control_response by consulting the permission_handler. SEC-011 is the load-
    bearing case: a handler that raises must be denied + logged, NEVER allowed —
    a buggy handler must not silently become a permissive policy."""

    def _can_use_tool(self, tool="Bash", inp=None) -> dict:
        return {
            "request_id": "rq-1",
            "request": {"subtype": "can_use_tool", "tool_name": tool,
                        "input": inp or {}},
        }

    def test_handler_response_is_forwarded(self):
        """A handler's allow/deny decision must be passed through verbatim into
        the control_response the child reads."""
        decision = {"behavior": "allow", "updatedInput": {"command": "ls"}}
        ch = ClaudeChannel(permission_handler=lambda tool, inp: decision)
        stdin = io.StringIO()
        ch._answer_control_request(stdin, self._can_use_tool())
        sent = json.loads(stdin.getvalue())
        assert sent["type"] == "control_response"
        assert sent["response"]["request_id"] == "rq-1"
        assert sent["response"]["response"] == decision

    def test_handler_raise_is_fail_closed_deny(self, capsys):
        """SEC-011: handler raises => deny payload (not allow, not empty) AND a
        stderr line naming the tool + exception, so the failure is auditable."""
        def boom(tool, inp):
            raise RuntimeError("handler bug")

        ch = ClaudeChannel(permission_handler=boom)
        stdin = io.StringIO()
        ch._answer_control_request(stdin, self._can_use_tool(tool="Write"))
        response = json.loads(stdin.getvalue())["response"]["response"]
        assert response["behavior"] == "deny"
        assert "RuntimeError" in response["message"]
        err = capsys.readouterr().err
        assert "permission_handler raised on tool='Write'" in err
        assert "RuntimeError" in err and "handler bug" in err

    def test_unknown_subtype_yields_empty_response(self):
        """A control_request we don't special-case (e.g. initialize ack) must
        get an empty response, not invoke the permission handler."""
        called = False

        def handler(tool, inp):
            nonlocal called
            called = True
            return {"behavior": "allow"}

        ch = ClaudeChannel(permission_handler=handler)
        stdin = io.StringIO()
        ch._answer_control_request(
            stdin, {"request_id": "rq-2", "request": {"subtype": "initialize"}})
        sent = json.loads(stdin.getvalue())["response"]
        assert sent["request_id"] == "rq-2"
        assert sent["response"] == {}
        assert not called


class TestReadUntilResult:
    """The NDJSON reader loop collects assistant text and answers permission
    requests until a `result` arrives or stdout closes. Its teardown branches
    (EOF, oversized line, unparseable line) must each end the turn cleanly
    rather than hang or crash silently."""

    def _stdout(self, *lines: str):
        class _Stdout:
            def __init__(self, q): self._q = list(q)
            def readline(self, *_a): return self._q.pop(0) if self._q else ""
        return _Stdout(lines)

    def test_answers_permission_then_returns_on_result(self):
        """Happy path: control_response acks pass through, a can_use_tool is
        answered via the handler, assistant text is collected, and the result
        message terminates the loop with its session_id + usage."""
        tools_seen: list[str] = []

        def handler(tool, inp):
            tools_seen.append(tool)
            return {"behavior": "allow"}

        ch = ClaudeChannel(permission_handler=handler)
        stdin, log = io.StringIO(), io.StringIO()
        stdout = self._stdout(
            json.dumps({"type": "control_response", "request_id": "init"}) + "\n",
            json.dumps({"type": "assistant",
                        "message": {"content": [{"type": "text", "text": "hi"}]}}) + "\n",
            json.dumps({"type": "control_request", "request_id": "rq",
                        "request": {"subtype": "can_use_tool",
                                    "tool_name": "Bash", "input": {}}}) + "\n",
            json.dumps({"type": "result", "session_id": "sid-9",
                        "result": "fallback", "usage": {"input_tokens": 5}}) + "\n",
        )
        text_parts: list[str] = []
        meta: dict = {}
        sid = ch._read_until_result(stdin, stdout, text_parts, log, meta)
        assert sid == "sid-9"
        assert text_parts == ["hi"]
        assert tools_seen == ["Bash"]
        assert "control_response" in stdin.getvalue()  # permission answered
        assert meta["input_tokens"] == 5

    def test_result_without_text_falls_back_to_result_field(self):
        """When no assistant text streamed, the result message's own `result`
        string is used as the turn output — otherwise the turn would be blank."""
        ch = ClaudeChannel()
        stdout = self._stdout(
            json.dumps({"type": "result", "session_id": "s",
                        "result": "the-answer", "usage": {}}) + "\n")
        parts: list[str] = []
        ch._read_until_result(io.StringIO(), stdout, parts, io.StringIO(), {})
        assert parts == ["the-answer"]

    def test_eof_ends_turn_without_result(self):
        """stdout closing before a result must return cleanly (no hang) — the
        captured session_id so far, which is None when no result was seen."""
        ch = ClaudeChannel()
        stdout = self._stdout()  # immediate EOF
        log = io.StringIO()
        sid = ch._read_until_result(io.StringIO(), stdout, [], log, {})
        assert sid is None
        assert "EOF" in log.getvalue()

    def test_unparseable_line_is_skipped(self):
        """A torn/garbage NDJSON line must be skipped, not abort the turn."""
        ch = ClaudeChannel()
        stdout = self._stdout(
            "this is not json\n",
            json.dumps({"type": "result", "session_id": "s2", "usage": {}}) + "\n")
        sid = ch._read_until_result(io.StringIO(), stdout, [], io.StringIO(), {})
        assert sid == "s2"

    def test_oversized_line_aborts_turn(self):
        """SEC-005: a line at the size cap with no newline is treated as a
        protocol error and aborts the turn, bounding peak memory."""
        ch = ClaudeChannel()
        stdout = self._stdout("x" * _NDJSON_MAX_LINE)  # at cap, no newline
        with pytest.raises(RuntimeError, match="NDJSON line cap"):
            ch._read_until_result(io.StringIO(), stdout, [], io.StringIO(), {})

    def test_early_session_id_fires_once_before_result(self):
        """on_session_id must fire the moment a session_id appears on the wire
        (system init), exactly once — the driver needs the fork's id well before
        the turn ends, but must not re-register it on every later message."""
        ch = ClaudeChannel()
        fired: list[str] = []
        stdout = self._stdout(
            json.dumps({"type": "system", "session_id": "early-1"}) + "\n",
            json.dumps({"type": "assistant", "session_id": "early-1",
                        "message": {"content": []}}) + "\n",
            json.dumps({"type": "result", "session_id": "early-1", "usage": {}}) + "\n",
        )
        ch._read_until_result(io.StringIO(), stdout, [], io.StringIO(), {},
                              on_session_id=fired.append)
        assert fired == ["early-1"]  # once, not three times

    def test_blank_lines_are_skipped(self):
        """Heartbeat/blank lines on the stream must be ignored, not parsed."""
        ch = ClaudeChannel()
        stdout = self._stdout(
            "   \n", "\n",
            json.dumps({"type": "result", "session_id": "s3", "usage": {}}) + "\n")
        assert ch._read_until_result(io.StringIO(), stdout, [], io.StringIO(), {}) == "s3"


class _FakeProc:
    """Stand-in for subprocess.Popen: only the attributes _run_one_turn reads."""

    def __init__(self, returncode=None):
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def kill(self):
        pass


class TestRunOneTurn:
    """_run_one_turn does the per-turn stdio dance (send prompt → read result)
    against a pooled process, then enforces invariants: a turn that never
    reports a session id, or exits non-zero with no output, must fail loud; a
    non-zero exit WITH output is downgraded to a warning so the envelope parser
    decides. Driven here with a fake process so no `claude` is spawned."""

    def _entry(self, *lines: str, returncode=None) -> _PooledProcess:
        class _Stdout:
            def __init__(self, q): self._q = list(q)
            def readline(self, *_a): return self._q.pop(0) if self._q else ""
        return _PooledProcess(
            proc=_FakeProc(returncode), stdin=io.StringIO(),
            stdout=_Stdout(lines), log=io.StringIO(),
            log_path="/tmp/turn.log", cwd="/tmp",
        )

    def test_happy_turn_returns_result(self):
        """A clean turn yields a TurnResult carrying the streamed text, session
        id, and usage counters the driver needs."""
        entry = self._entry(
            json.dumps({"type": "assistant",
                        "message": {"content": [{"type": "text", "text": "done"}]}}) + "\n",
            json.dumps({"type": "result", "session_id": "sid-ok",
                        "usage": {"input_tokens": 7, "output_tokens": 3},
                        "total_cost_usd": 0.01}) + "\n",
        )
        r = ClaudeChannel()._run_one_turn(entry, "go", timeout=10,
                                          on_session_id=None, do_handshake=False)
        assert r.session_id == "sid-ok"
        assert r.text == "done"
        assert r.input_tokens == 7 and r.output_tokens == 3
        assert entry.session_id == "sid-ok"  # entry updated for pool reuse

    def test_no_session_id_raises(self):
        """A result without a session id leaves the driver unable to locate the
        child — must raise, not return an unusable TurnResult."""
        entry = self._entry(
            json.dumps({"type": "result", "result": "hi", "usage": {}}) + "\n")
        with pytest.raises(RuntimeError, match="without reporting a session id"):
            ClaudeChannel()._run_one_turn(entry, "go", timeout=10,
                                          on_session_id=None, do_handshake=False)

    def test_nonzero_exit_without_output_raises(self):
        """claude exited non-zero AND produced no text => nothing to salvage;
        fail loud rather than hand back an empty turn."""
        entry = self._entry(
            json.dumps({"type": "result", "session_id": "s", "usage": {}}) + "\n",
            returncode=1)
        with pytest.raises(RuntimeError, match="returncode=1"):
            ClaudeChannel()._run_one_turn(entry, "go", timeout=10,
                                          on_session_id=None, do_handshake=False)

    def test_nonzero_exit_with_output_warns_and_returns(self):
        """Non-zero exit WITH output is downgraded to a logged warning — the
        envelope parser, not the exit code, decides the turn's outcome."""
        entry = self._entry(
            json.dumps({"type": "assistant",
                        "message": {"content": [{"type": "text", "text": "partial"}]}}) + "\n",
            json.dumps({"type": "result", "session_id": "s", "usage": {}}) + "\n",
            returncode=2)
        r = ClaudeChannel()._run_one_turn(entry, "go", timeout=10,
                                          on_session_id=None, do_handshake=False)
        assert r.text == "partial"
        assert "returncode=2" in entry.log.getvalue()

    def test_preallocated_session_mismatch_raises(self):
        """If the wire session id differs from the UUID we pinned via
        --session-id, Claude Code's contract changed and the subtree would
        cross-fork — refuse rather than write a corrupted report."""
        entry = self._entry(
            json.dumps({"type": "result", "session_id": "actual", "usage": {}}) + "\n")
        with pytest.raises(RuntimeError, match="pre-allocated"):
            ClaudeChannel()._run_one_turn(
                entry, "go", timeout=10, on_session_id=None,
                do_handshake=False, preallocated_session_id="expected")

    def test_handshake_sent_once_on_first_turn(self):
        """The stream-json `initialize` handshake must be sent before the first
        user message on a fresh process, and the entry marked initialized so a
        later reuse turn (do_handshake=False) never re-sends it."""
        entry = self._entry(
            json.dumps({"type": "result", "session_id": "s",
                        "result": "ok", "usage": {}}) + "\n")
        ClaudeChannel()._run_one_turn(entry, "go", timeout=10,
                                      on_session_id=None, do_handshake=True)
        assert "initialize" in entry.stdin.getvalue()
        assert entry.initialized is True


class TestAllowAll:
    """allow_all is the default permission_handler — it must approve every tool
    so the out-of-the-box channel isn't blocked on permission prompts, and must
    echo the input back as updatedInput (the shape the CLI expects)."""

    def test_allows_and_echoes_input(self, capsys):
        decision = allow_all("Bash", {"command": "ls"})
        assert decision == {"behavior": "allow",
                            "updatedInput": {"command": "ls"}}
        assert "allowing Bash" in capsys.readouterr().err


class TestProcessLogPath:
    """SEC-004: a `claude` subprocess's log must land in a private location.
    Inside an active invocation it goes under the stamped root log dir (so it
    sits next to report.yaml); outside one it falls back to a mode-0600
    tempfile. Picking the wrong branch would either lose logs or leak them
    into a world-readable temp path."""

    def test_uses_root_log_dir_when_invocation_active(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ch_mod._env, "root_identity",
                            lambda: ("inv-7", str(tmp_path)))
        path = _process_log_path("abc")
        assert path.startswith(str(tmp_path / "inv-7" / "process_logs"))
        assert path.endswith(".log")
        assert (tmp_path / "inv-7" / "process_logs").is_dir()

    def test_falls_back_to_tempfile_outside_invocation(self, monkeypatch):
        monkeypatch.setattr(ch_mod._env, "root_identity", lambda: None)
        path = _process_log_path("abc")
        # A real, private (0600) file was created as the fallback.
        import os
        import stat
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
            assert mode == 0o600
        finally:
            os.unlink(path)


class _ClosableProc:
    """Fake Popen exposing only what _PooledProcess.close drives, with knobs to
    force the terminate-timeout → kill fallback and a kill that also fails."""

    def __init__(self, *, terminate_times_out=False, kill_wait_raises=False):
        self.returncode = 0
        self._terminate_times_out = terminate_times_out
        self._kill_wait_raises = kill_wait_raises
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        if timeout is not None and self._terminate_times_out:
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
        if timeout is None and self._kill_wait_raises:
            raise OSError("no such process")
        return 0


class _RaisingStdin:
    def close(self):
        raise OSError("broken pipe")


class _RaisingLog:
    def write(self, _s):
        raise OSError("disk full")

    def close(self):
        raise OSError("disk full")

    def flush(self):
        pass


class TestPooledProcessLifecycle:
    """_PooledProcess.close must be robust: it runs during eviction/shutdown
    where the child may already be dead, stdin may be broken, or the log fd
    gone. Every failure mode must be swallowed so teardown of one process never
    aborts teardown of the pool. is_alive must also report a closed entry as
    dead so the pool drops it."""

    def _entry(self, proc, *, stdin=None, log=None) -> _PooledProcess:
        return _PooledProcess(
            proc=proc, stdin=stdin or io.StringIO(),
            stdout=io.StringIO(), log=log or io.StringIO(),
            log_path="/tmp/x.log", cwd="/tmp",
        )

    def test_is_alive_false_when_closed(self):
        entry = self._entry(_ClosableProc())
        entry.closed = True
        assert entry.is_alive() is False

    def test_close_is_idempotent(self):
        entry = self._entry(_ClosableProc())
        entry.close()
        assert entry.closed
        # Second call returns early without touching the (now stale) proc.
        entry.proc.terminated = False
        entry.close()
        assert entry.proc.terminated is False

    def test_close_swallows_stdin_close_error(self):
        entry = self._entry(_ClosableProc(), stdin=_RaisingStdin())
        entry.close()  # OSError on stdin.close must not propagate
        assert entry.proc.terminated

    def test_close_falls_back_to_kill_on_terminate_timeout(self):
        proc = _ClosableProc(terminate_times_out=True)
        self._entry(proc).close()
        assert proc.terminated and proc.killed

    def test_close_swallows_kill_wait_error(self):
        proc = _ClosableProc(terminate_times_out=True, kill_wait_raises=True)
        self._entry(proc).close()  # OSError from the post-kill wait is swallowed
        assert proc.killed

    def test_close_swallows_log_write_error(self):
        self._entry(_ClosableProc(), log=_RaisingLog()).close()  # no raise


class TestPoolModuleHelpers:
    """The module-level pool is lazily created on first use (so tests can swap
    it) and registers an atexit teardown exactly once. max_size and evict-of-
    a-missing-id are the small accessor/no-op branches the integration tests
    don't reach."""

    def test_max_size_exposes_cap(self):
        assert ClaudePool(max_size=5).max_size == 5

    def test_evict_missing_id_is_noop(self):
        ClaudePool(max_size=2).evict("never-registered")  # must not raise

    def test_get_pool_lazily_creates_and_registers_atexit(self, monkeypatch):
        registered: list = []
        monkeypatch.setattr(ch_mod.atexit, "register", registered.append)
        monkeypatch.setattr(ch_mod, "_pool", None)
        pool = _get_pool()
        assert isinstance(pool, ClaudePool)
        assert _get_pool() is pool  # cached, not re-created
        assert registered, "atexit teardown must be registered on creation"

    def test_shutdown_pool_tears_down_existing(self, monkeypatch):
        calls: list = []

        class _FakePool:
            def shutdown(self):
                calls.append(1)

        monkeypatch.setattr(ch_mod, "_pool", _FakePool())
        shutdown_pool()
        assert calls == [1]

    def test_shutdown_pool_noop_when_no_pool(self, monkeypatch):
        monkeypatch.setattr(ch_mod, "_pool", None)
        shutdown_pool()  # must not raise


class TestRunTurnArgValidation:
    """SEC-013: run_turn rejects a bad mode or a non-UUID session id BEFORE any
    subprocess spawn — these values flow into the `claude` argv, so validating
    early stops a typo or hostile caller from reaching the CLI."""

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValueError, match="invalid run_turn mode"):
            ClaudeChannel().run_turn("", "p", mode="bogus")

    def test_non_uuid_source_session_rejected(self):
        with pytest.raises(ValueError, match="must be a UUID"):
            ClaudeChannel().run_turn("not-a-uuid", "p", mode="resume")

    def test_non_uuid_preallocation_rejected(self):
        with pytest.raises(ValueError, match="preallocated_session_id"):
            ClaudeChannel().run_turn("", "p", mode="fresh",
                                     preallocated_session_id="not-a-uuid")


class TestLogSemWait:
    """_log_sem_wait records contention only when a turn actually waited on the
    concurrency semaphore (>0.5s), and must never let a logging failure escape
    into the turn."""

    def test_short_wait_not_logged(self):
        log = io.StringIO()
        ClaudeChannel._log_sem_wait(log, 0.1)
        assert log.getvalue() == ""

    def test_long_wait_logged(self):
        log = io.StringIO()
        ClaudeChannel._log_sem_wait(log, 1.5)
        assert "semaphore-wait: 1.50s" in log.getvalue()

    def test_log_failure_swallowed(self):
        ClaudeChannel._log_sem_wait(_RaisingLog(), 2.0)  # OSError must not escape


class _FakeStderr:
    def __init__(self, lines):
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)


class _FakeSpawnPopen:
    """Minimal Popen stand-in for exercising _spawn without a real `claude`."""

    def __init__(self, *_a, stderr_lines=(), **_k):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()
        self.stderr = _FakeStderr(list(stderr_lines))
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


class TestSpawn:
    """_spawn builds the process, stamps the child's own session UUID into its
    env, and starts a stderr drain thread that caps total bytes (SEC-005). The
    drain runs in a daemon thread, so the test polls the log file until the cap
    message lands."""

    def test_spawn_stamps_session_and_drains_capped_stderr(self, monkeypatch):
        # Tiny cap so a couple of lines trip the SEC-005 ceiling deterministically.
        monkeypatch.setattr(ch_mod, "_STDERR_LOG_CAP", 8)
        captured = {}

        def fake_popen(*a, **k):
            proc = _FakeSpawnPopen(stderr_lines=["aa\n", "bbbbbbbb\n", "cc\n"])
            captured["env"] = k.get("env", {})
            return proc

        monkeypatch.setattr(ch_mod.subprocess, "Popen", fake_popen)
        uuid = "00000000-0000-0000-0000-0000000000ab"
        entry = ClaudeChannel()._spawn(
            "", "fresh", "/tmp", None, preallocated_session_id=uuid)
        try:
            # Child's own-session env var was stamped from the preallocated UUID.
            assert captured["env"][ch_mod._env.ENV_OWN_SESSION] == uuid

            from pathlib import Path
            deadline = time.time() + 2.0
            log_text = ""
            while time.time() < deadline:
                log_text = Path(entry.log_path).read_text()
                if "capped at" in log_text:
                    break
                time.sleep(0.01)
            assert "STDERR: aa" in log_text       # first line written
            assert "capped at 8 bytes" in log_text  # SEC-005 cap tripped
        finally:
            entry.close()

    def test_spawn_failure_wrapped_as_runtime_error(self, monkeypatch):
        """A Popen that fails (e.g. `claude` not on PATH) must surface as a
        clear RuntimeError, not a bare OSError from deep in spawn."""
        def boom(*_a, **_k):
            raise OSError("no claude binary")

        monkeypatch.setattr(ch_mod.subprocess, "Popen", boom)
        with pytest.raises(RuntimeError, match="Failed to start claude CLI"):
            ClaudeChannel().run_turn("", "hi", mode="fresh")


class _SlowStdout:
    """readline blocks past the turn timeout, then EOFs — forces the watchdog
    to fire before the read completes."""

    def __init__(self, delay: float):
        self._delay = delay

    def readline(self, *_a):
        time.sleep(self._delay)
        return ""


class _KillRaisesProc:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def kill(self):
        raise OSError("already gone")


class TestWatchdogTimeout:
    """The per-turn watchdog kills the process and raises TurnTimeout when a
    turn overruns. A kill that itself fails (child already dead) must be
    swallowed — the timeout is still reported with whatever partial text
    accumulated."""

    def test_turn_timeout_raised_and_kill_error_swallowed(self):
        entry = _PooledProcess(
            proc=_KillRaisesProc(), stdin=io.StringIO(),
            stdout=_SlowStdout(0.3), log=io.StringIO(),
            log_path="/tmp/x.log", cwd="/tmp",
        )
        with pytest.raises(TurnTimeout):
            ClaudeChannel()._run_one_turn(
                entry, "go", timeout=0.05, on_session_id=None,
                do_handshake=False)
        assert "TIMEOUT after" in entry.log.getvalue()


class TestReadUntilResultRemainingBranches:
    """Two reader-loop branches not hit by the happy-path tests: a message that
    carries no usable session_id must not fire on_session_id, and an assistant
    block that isn't text must be skipped rather than appended."""

    def _stdout(self, *lines: str):
        class _Stdout:
            def __init__(self, q):
                self._q = list(q)

            def readline(self, *_a):
                return self._q.pop(0) if self._q else ""
        return _Stdout(lines)

    def test_message_without_session_id_does_not_fire_callback(self):
        fired: list = []
        ch = ClaudeChannel()
        stdout = self._stdout(
            # on_session_id set, but this message has no session_id → skip.
            json.dumps({"type": "control_response", "request_id": "x"}) + "\n",
            json.dumps({"type": "result", "session_id": "s", "usage": {}}) + "\n",
        )
        ch._read_until_result(io.StringIO(), stdout, [], io.StringIO(), {},
                              on_session_id=fired.append)
        # Fires once, on the result's session_id — never on the id-less message.
        assert fired == ["s"]

    def test_non_text_assistant_block_is_skipped(self):
        ch = ClaudeChannel()
        parts: list = []
        stdout = self._stdout(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash"},
                {"type": "text", "text": "kept"},
            ]}}) + "\n",
            json.dumps({"type": "result", "session_id": "s", "usage": {}}) + "\n",
        )
        ch._read_until_result(io.StringIO(), stdout, parts, io.StringIO(), {})
        assert parts == ["kept"]  # tool_use block dropped, text block kept
