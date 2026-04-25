"""Channel: the seam between the runtime and an LLM session.

A Channel runs one LLM turn — either forking a parent session (the first
turn) or resuming an existing session (subsequent turns) — and returns the
agent's text output plus the session id the CLI assigned.

Two implementations:
- ClaudeChannel: spawns `claude` and speaks the stream-json NDJSON protocol.
- ScriptedChannel: returns canned text for a given (session_id, prompt). Used
  by tests so the entire driver/state machine can be exercised without ever
  spawning a subprocess.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, Union


# Global concurrency cap for concurrent claude subprocess invocations.
# Each ClaudeChannel.run_turn() spawns a short-lived `claude` subprocess
# (one turn per spawn), so this bounds the number of *active* claude
# processes system-wide — not the number of in-flight logical calls.
# The recursive callstack can logically have many thousands of pending
# turns; the semaphore ensures only N of them are physically running at
# once. Set via env `CALLSTACK_MAX_CONCURRENT_FORKS` (default 8).
#
# Each `claude` process consumes ~0.5-2 GB RSS, so on a typical 16-64 GB
# machine the safe ceiling is 8-30. Default 8 is conservative; bump for
# larger machines or reduce for tight memory conditions.
_MAX_CONCURRENT_FORKS = int(os.environ.get("CALLSTACK_MAX_CONCURRENT_FORKS", "8"))
_FORK_SEMAPHORE = threading.BoundedSemaphore(value=_MAX_CONCURRENT_FORKS)


@dataclass(frozen=True)
class TurnResult:
    text: str
    session_id: str
    duration: float
    # Usage + reproducibility fields. Populated from the stream-json `result`
    # message by ClaudeChannel; ScriptedChannel passes "" / 0 / 0.0 for these
    # since the test harness doesn't simulate a real provider response.
    api_request_id: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    total_cost_usd: float


class TurnTimeout(Exception):
    """Raised when an LLM turn exceeds its timeout. Carries any partial text."""
    def __init__(self, message: str, partial: str = ""):
        super().__init__(message)
        self.partial = partial


PermissionHandler = Callable[[str, dict], dict]


def allow_all(tool_name: str, input_data: dict) -> dict:
    print(f"[callstack] Permission: allowing {tool_name}", file=sys.stderr)
    return {"behavior": "allow", "updatedInput": input_data}


class Channel(Protocol):
    def run_turn(
        self,
        source_session_id: str,
        prompt: str,
        *,
        fork: bool,
        cwd: Optional[str] = None,
        timeout: int = 300,
        extra_env: Optional[dict] = None,
        on_session_id: Optional[Callable[[str], None]] = None,
    ) -> TurnResult: ...


# --------------------------------------------------------------------------
# Real Claude CLI channel
# --------------------------------------------------------------------------

class ClaudeChannel:
    """Spawns `claude` and exchanges NDJSON over stdio."""

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        permission_mode: str = "default",
        permission_handler: Optional[PermissionHandler] = None,
        env: Optional[dict] = None,
    ):
        self._model = model
        self._permission_mode = permission_mode
        self._handler = permission_handler or allow_all
        self._env_extra = env or {}

    def run_turn(
        self,
        source_session_id: str,
        prompt: str,
        *,
        fork: bool,
        cwd: Optional[str] = None,
        timeout: int = 300,
        extra_env: Optional[dict] = None,
        on_session_id: Optional[Callable[[str], None]] = None,
    ) -> TurnResult:
        cmd = self._build_cmd(source_session_id, fork)
        effective_cwd = cwd or os.getcwd()
        env = {**os.environ, **self._env_extra, **(extra_env or {})}

        log_path = f"/tmp/callstack_{source_session_id[:8]}_{uuid.uuid4().hex[:8]}.log"
        log = open(log_path, "w")
        log.write(f"cmd: {' '.join(cmd)}\ncwd: {effective_cwd}\n")
        log.flush()
        print(f"[callstack] turn (fork={fork}, source={source_session_id[:8]}..., "
              f"cwd={effective_cwd}, log={log_path})", file=sys.stderr)

        start = time.time()
        # Gate Popen behind the global semaphore so system-wide concurrent
        # claude-subprocess count stays bounded even under deep recursive
        # callstacks. We hold the slot for the duration of the turn
        # (including the subprocess wait below); release on all exit paths.
        _sem_wait_start = time.time()
        _FORK_SEMAPHORE.acquire()
        _sem_wait = time.time() - _sem_wait_start
        if _sem_wait > 0.5:
            log.write(f"semaphore-wait: {_sem_wait:.2f}s "
                      f"(cap={_MAX_CONCURRENT_FORKS})\n")
            log.flush()
        _released = False
        def _release_slot():
            nonlocal _released
            if not _released:
                _FORK_SEMAPHORE.release()
                _released = True

        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, cwd=effective_cwd, env=env,
            )
        except Exception as e:
            _release_slot()
            log.close()
            raise RuntimeError(f"Failed to start claude CLI: {e}") from e

        # PIPE was passed for all three streams, so they are guaranteed non-None.
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        stdin, stdout, stderr = proc.stdin, proc.stdout, proc.stderr

        # Drain stderr in the background so a full pipe can't deadlock the child.
        def _drain_stderr():
            for line in stderr:
                log.write(f"STDERR: {line}")
                log.flush()
        threading.Thread(target=_drain_stderr, daemon=True).start()

        # Watchdog kills the child if a turn runs too long.
        timed_out = threading.Event()
        cancel = threading.Event()
        def _watchdog():
            if not cancel.wait(timeout):
                timed_out.set()
                log.write(f"TIMEOUT after {timeout}s\n"); log.flush()
                try: proc.kill()
                except OSError: pass
        threading.Thread(target=_watchdog, daemon=True).start()

        text_parts: list[str] = []
        session_id: Optional[str] = None
        result_meta: dict = {}

        try:
            self._handshake(stdin, log)
            self._send_user_message(stdin, prompt, log)
            session_id = self._read_until_result(
                stdin, stdout, text_parts, log, result_meta,
                on_session_id=on_session_id,
            )
        finally:
            cancel.set()
            try: stdin.close()
            except OSError: pass
            try:
                proc.terminate(); proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                proc.kill(); proc.wait()
            log.write(f"Process exited with code {proc.returncode}\n")
            log.close()
            _release_slot()

        text = "".join(text_parts)
        if timed_out.is_set():
            raise TurnTimeout(f"turn timed out after {timeout}s", partial=text)
        if not session_id:
            raise RuntimeError(
                f"claude CLI exited without reporting a session id "
                f"(returncode={proc.returncode}, log={log_path})"
            )

        return TurnResult(
            text=text,
            session_id=session_id,
            duration=time.time() - start,
            api_request_id=result_meta.get("api_request_id", ""),
            input_tokens=result_meta.get("input_tokens", 0),
            output_tokens=result_meta.get("output_tokens", 0),
            cache_read_tokens=result_meta.get("cache_read_tokens", 0),
            cache_creation_tokens=result_meta.get("cache_creation_tokens", 0),
            total_cost_usd=result_meta.get("total_cost_usd", 0.0),
        )

    # ---- private helpers ----

    def _build_cmd(self, source_session_id: str, fork: bool) -> list[str]:
        cmd = [
            "claude",
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--verbose",
            "--resume", source_session_id,
            "--permission-prompt-tool", "stdio",
            "--permission-mode", self._permission_mode,
        ]
        if fork:
            cmd.append("--fork-session")
        if self._model:
            cmd.extend(["--model", self._model])
        return cmd

    @staticmethod
    def _send(stdin, obj: dict) -> None:
        stdin.write(json.dumps(obj) + "\n")
        stdin.flush()

    def _handshake(self, stdin, log) -> None:
        log.write("→ initialize\n"); log.flush()
        self._send(stdin, {
            "type": "control_request",
            "request_id": f"req_init_{uuid.uuid4().hex[:8]}",
            "request": {"subtype": "initialize", "hooks": None},
        })

    def _send_user_message(self, stdin, prompt: str, log) -> None:
        log.write(f"→ user message ({len(prompt)} chars)\n"); log.flush()
        self._send(stdin, {
            "type": "user",
            "session_id": "",
            "message": {"role": "user", "content": prompt},
            "parent_tool_use_id": None,
        })

    def _read_until_result(self, stdin, stdout, text_parts: list, log,
                            result_meta: dict,
                            *,
                            on_session_id: Optional[Callable[[str], None]] = None,
                            ) -> Optional[str]:
        """Read NDJSON lines, collecting assistant text and answering permission
        requests, until a `result` message arrives or stdout closes.

        `result_meta` is populated with the `result` message's usage counters
        and `uuid` (Anthropic request-id) so the caller can build a complete
        TurnResult without re-parsing.

        `on_session_id`, if supplied, is invoked the moment we observe a
        session_id on the wire — typically the `system init` message at the
        very start of the turn, well before the final `result`. Lets the
        driver register the new fork's session_id in its tree (and the
        progress reporter) without waiting for the full turn to complete.
        """
        session_id: Optional[str] = None
        early_id_fired = False
        while True:
            # readline (not iter) — the iterator's read-ahead delays delivery and hangs.
            raw = stdout.readline()
            if not raw:
                log.write("← EOF\n"); log.flush()
                return session_id
            raw = raw.strip()
            if not raw:
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.write(f"← unparseable: {raw[:200]}\n"); log.flush()
                continue

            mtype = msg.get("type")
            log.write(f"← {mtype}\n"); log.flush()

            # Fire early-session-id callback the moment we see one on any
            # message type (system init carries it first; assistant messages
            # also include it in stream-json output).
            if not early_id_fired and on_session_id is not None:
                early_sid = msg.get("session_id")
                if isinstance(early_sid, str) and early_sid:
                    early_id_fired = True
                    try:
                        on_session_id(early_sid)
                    except Exception:
                        # Never let an observer error abort the turn.
                        log.write("on_session_id callback raised; ignoring\n")
                        log.flush()

            if mtype == "control_response":
                continue
            if mtype == "control_request":
                self._answer_control_request(stdin, msg)
                continue
            if mtype == "assistant":
                for block in msg.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        text_parts.append(block["text"])
                continue
            if mtype == "result":
                session_id = msg.get("session_id")
                if not text_parts and msg.get("result"):
                    text_parts.append(msg["result"])
                usage = msg.get("usage") or {}
                result_meta["api_request_id"] = msg.get("uuid", "")
                result_meta["input_tokens"] = usage.get("input_tokens", 0)
                result_meta["output_tokens"] = usage.get("output_tokens", 0)
                result_meta["cache_read_tokens"] = usage.get("cache_read_input_tokens", 0)
                result_meta["cache_creation_tokens"] = usage.get("cache_creation_input_tokens", 0)
                result_meta["total_cost_usd"] = msg.get("total_cost_usd", 0.0)
                return session_id

    def _answer_control_request(self, stdin, msg: dict) -> None:
        request = msg.get("request", {})
        request_id = msg.get("request_id", "")
        subtype = request.get("subtype", "")
        if subtype == "can_use_tool":
            response = self._handler(request.get("tool_name", ""), request.get("input", {}))
        else:
            response = {}
        self._send(stdin, {
            "type": "control_response",
            "response": {"subtype": "success", "request_id": request_id, "response": response},
        })


# --------------------------------------------------------------------------
# Test channel
# --------------------------------------------------------------------------

ScriptedResponse = Callable[[str, str, bool], TurnResult]
ScriptedEntry = Union[tuple[str, str], ScriptedResponse]


@dataclass
class ScriptedChannel:
    """Test channel that returns scripted text for each turn.

    Each entry in `responses` is either a `(text, session_id)` pair or a
    callable invoked with `(source_session_id, prompt, fork)`. `log` records
    every call so tests can assert on the full sequence."""

    responses: list[ScriptedEntry] = field(default_factory=list)
    log: list[tuple[str, str, bool]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def respond(self, text: str, session_id: str = "scripted-session") -> "ScriptedChannel":
        with self._lock:
            self.responses.append((text, session_id))
        return self

    def respond_with(self, fn: ScriptedResponse) -> "ScriptedChannel":
        with self._lock:
            self.responses.append(fn)
        return self

    def run_turn(
        self,
        source_session_id: str,
        prompt: str,
        *,
        fork: bool,
        cwd: Optional[str] = None,
        timeout: int = 300,
        extra_env: Optional[dict] = None,
        on_session_id: Optional[Callable[[str], None]] = None,
    ) -> TurnResult:
        _ = (cwd, timeout, extra_env)  # accepted for parity with ClaudeChannel
        with self._lock:
            if not self.responses:
                raise AssertionError(
                    f"ScriptedChannel exhausted; "
                    f"unscripted call: source={source_session_id}, fork={fork}"
                )
            self.log.append((source_session_id, prompt, fork))
            nxt = self.responses.pop(0)
        if callable(nxt):
            result = nxt(source_session_id, prompt, fork)
            if on_session_id is not None and result.session_id:
                try: on_session_id(result.session_id)
                except Exception: pass
            return result
        text, session_id = nxt
        if on_session_id is not None and session_id:
            try: on_session_id(session_id)
            except Exception: pass
        return TurnResult(
            text=text, session_id=session_id, duration=0.0,
            api_request_id="", input_tokens=0, output_tokens=0,
            cache_read_tokens=0, cache_creation_tokens=0, total_cost_usd=0.0,
        )
