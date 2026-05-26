"""Channel: the seam between the runtime and an LLM session.

A Channel runs one LLM turn — either forking a parent session (the first
turn) or resuming an existing session (subsequent turns) — and returns the
agent's text output plus the session id the CLI assigned.

## Process-per-session pool (PERF-D)

To avoid paying the ~0.5–2 GB RSS / ~1 s cold-start cost of a fresh
`claude` subprocess on every turn, one long-lived process is kept per
session_id and reused across that session's consecutive resume-mode
turns. The pool itself (registry, LRU eviction, lifecycle) lives in
`channel_pool`; ClaudeChannel just drives `_get_pool()`. Fork/fresh
turns always spawn (they create a new session id); resume turns reuse
on a pool hit and spawn on a miss.

Concurrency is two-level (PERF-H):
  * `_SPAWN_SEMAPHORE`     — bounded by `CALLSTACK_MAX_CONCURRENT_FORKS`,
                              held only when spawning a new claude
                              process. The real memory ceiling.
  * `_IN_FLIGHT_SEMAPHORE` — bounded by `CALLSTACK_MAX_IN_FLIGHT_TURNS`
                              (default 2× the spawn cap), held for every
                              turn. Pool-hit (reuse) turns acquire ONLY
                              this one, so resume-mode parallelism
                              isn't gated on cold-start capacity.

In-use pooled processes (per-process lock held) are skipped during
eviction — the pool may exceed cap briefly until a turn finishes.

The pool is torn down at interpreter exit via `atexit`, or explicitly
via `Caller.close()` / `shutdown_pool()`.

## Implementations

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
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol, TextIO

from . import env as _env

# The process-per-session pool lives in its own module (see channel_pool).
# Re-exported here so the historical public surface
# (`from agent_callstack.channel import shutdown_pool`, etc.) keeps working
# and ClaudeChannel can drive the pool without an extra import hop.
from .channel_pool import (  # noqa: F401
    _MAX_CONCURRENT_FORKS,
    ClaudePool,
    _get_pool,
    _PooledProcess,
    shutdown_pool,
)


def _process_log_path(stem: str) -> str:
    """Private log path for a `claude` subprocess (SEC-004).

    Prefer the current callstack invocation's log dir when the runtime
    has stamped it via env (`CALLSTACK_ROOT_LOG_DIR` + `CALLSTACK_ROOT_INVOKE_ID`)
    so logs land alongside report.yaml / call_trace.jsonl under a tree
    the OS already keeps in the user's private home. Fall back to a
    mode-0600 NamedTemporaryFile when the env isn't set (CLI / library
    use outside an active invocation)."""
    root = _env.root_identity()
    if root is not None:
        invoke_id, root_dir = root
        proc_dir = Path(root_dir) / invoke_id / "process_logs"
        proc_dir.mkdir(parents=True, exist_ok=True)
        return str(proc_dir / f"callstack_{stem}_{uuid.uuid4().hex[:8]}.log")
    # NamedTemporaryFile(delete=False) creates the file with mode 0600,
    # unlike a bare `open()` which inherits the (typically 022) umask.
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        prefix=f"callstack_{stem}_",
        suffix=".log",
        delete=False,
    )
    path = tmp.name
    tmp.close()
    return path


# Two-level concurrency cap (PERF-H):
#
#   _SPAWN_SEMAPHORE      — bounds COLD STARTS. Held only when actually
#                           spawning a new `claude` subprocess. Each spawn
#                           costs ~0.5–2 GB RSS, so this is the real
#                           memory-ceiling knob.
#                           Env: CALLSTACK_MAX_CONCURRENT_FORKS (default 8).
#
#   _IN_FLIGHT_SEMAPHORE  — bounds CONCURRENT TURNS (spawn + pool-hit).
#                           Held for the full turn duration. Pool-hit turns
#                           reuse an existing process so they don't pay the
#                           RSS cost again; they only need a CPU/network
#                           slot. Default is 2× the spawn cap.
#                           Env: CALLSTACK_MAX_IN_FLIGHT_TURNS (default
#                           2 × CALLSTACK_MAX_CONCURRENT_FORKS).
#
# Before PERF-H a single semaphore double-capped on the same number, so a
# pool-hit (no spawn) still had to wait for a "spawn slot" even though no
# spawn was happening. Splitting raises usable parallelism for resume-mode
# turns without raising peak RSS.
# `_MAX_CONCURRENT_FORKS` is imported from channel_pool (it sizes both the
# pool and this spawn semaphore off the same env knob).
_MAX_IN_FLIGHT_TURNS = _env.max_in_flight_turns()
_SPAWN_SEMAPHORE = threading.BoundedSemaphore(value=_MAX_CONCURRENT_FORKS)
_IN_FLIGHT_SEMAPHORE = threading.BoundedSemaphore(value=_MAX_IN_FLIGHT_TURNS)

# SEC-005: bound the worst-case NDJSON line and per-turn stderr drain.
# `claude` lines are normally <100 KB; the cap exists so a malicious or
# stuck child can't drive parent RSS without bound.
_NDJSON_MAX_LINE = 4 * 1024 * 1024  # 4 MiB per line
_STDERR_LOG_CAP = 16 * 1024 * 1024  # 16 MiB per turn before truncate

# SEC-013: argv-input validation. `claude --permission-mode <X>` and
# `claude --resume <UUID>` accept these from us; reject anything outside
# the known set / UUID shape before subprocess spawn.
_VALID_PERMISSION_MODES = frozenset(
    {
        "default",
        "acceptEdits",
        "plan",
        "bypassPermissions",
    }
)
# UUID shape regex is owned by `session.py` (the lowest-level module that
# knows what a session id looks like); import it here so the same pattern
# governs both `--session-id` argv validation and on-disk session lookups.
from .session import _UUID_RE  # noqa: E402


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


def _fire_on_session_id(cb: Callable[[str], None], sid: str, log: Optional[TextIO] = None) -> None:
    """SEC-011: invoke an advisory on_session_id callback, surfacing any
    exception to stderr (and the per-turn `log`, when given) instead of
    swallowing it silently. The callback is an advisory observer, so a raise
    is logged and never propagated into the turn."""
    try:
        cb(sid)
    except Exception as e:
        msg = f"on_session_id callback raised: {type(e).__name__}: {str(e)[:200]}"
        print(f"[callstack] {msg}", file=sys.stderr)
        if log is not None:
            log.write(msg + "\n")
            log.flush()


class Channel(Protocol):
    def run_turn(
        self,
        source_session_id: str,
        prompt: str,
        *,
        mode: str,
        cwd: Optional[str] = None,
        timeout: int = 300,
        extra_env: Optional[dict] = None,
        on_session_id: Optional[Callable[[str], None]] = None,
        preallocated_session_id: Optional[str] = None,
    ) -> TurnResult: ...


# --------------------------------------------------------------------------
# Real Claude CLI channel
# --------------------------------------------------------------------------


class ClaudeChannel:
    """Spawns `claude` and exchanges NDJSON over stdio. Maintains a
    process-per-session pool — see module docstring."""

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        permission_mode: str = "default",
        permission_handler: Optional[PermissionHandler] = None,
        env: Optional[dict] = None,
    ):
        # SEC-013: validate permission_mode against the known set so
        # typos / hostile callers can't smuggle arbitrary values into
        # the claude argv.
        if permission_mode not in _VALID_PERMISSION_MODES:
            raise ValueError(
                f"invalid permission_mode {permission_mode!r}; expected one of {sorted(_VALID_PERMISSION_MODES)}"
            )
        self._model = model
        self._permission_mode = permission_mode
        self._handler = permission_handler or allow_all
        self._env_extra = env or {}

    def run_turn(
        self,
        source_session_id: str,
        prompt: str,
        *,
        mode: str,
        cwd: Optional[str] = None,
        timeout: int = 300,
        extra_env: Optional[dict] = None,
        on_session_id: Optional[Callable[[str], None]] = None,
        preallocated_session_id: Optional[str] = None,
    ) -> TurnResult:
        # ARCH-11: env precedence is `os.environ < self._env_extra (set at
        # ClaudeChannel construction) < extra_env (per-turn override)`.
        # The driver uses `extra_env` only to inject CALLSTACK_FRAME_KEY
        # per spawn, so right-most wins is the desired shape.
        if mode not in ("fork", "fresh", "resume"):
            raise ValueError(f"invalid run_turn mode: {mode!r}")
        # SEC-013: when a session id is supplied (fork/resume), it must be
        # a real UUID. `fresh` mode passes an empty string and is exempt.
        if source_session_id and not _UUID_RE.fullmatch(source_session_id):
            raise ValueError(f"invalid source_session_id {source_session_id!r}; must be a UUID")
        if preallocated_session_id is not None and not _UUID_RE.fullmatch(preallocated_session_id):
            raise ValueError(f"invalid preallocated_session_id {preallocated_session_id!r}; must be a UUID")
        effective_cwd = cwd or os.getcwd()
        pool = _get_pool()

        # Reuse a pooled process for resume mode against a known session_id.
        # Fork/fresh always spawn fresh (they CREATE a new session id, which
        # we can only pool under after the result message reports it).
        pooled: Optional[_PooledProcess] = None
        if mode == "resume" and source_session_id:
            pooled = pool.acquire(source_session_id)

        # Two-level concurrency cap (PERF-H):
        #   _IN_FLIGHT_SEMAPHORE caps total concurrent turns (spawn + reuse).
        #   _SPAWN_SEMAPHORE caps cold starts only — acquired below inside
        #   _fresh_spawn_turn / _spawn when actually spawning. A pool-hit
        #   does NOT acquire the spawn semaphore, freeing parallelism for
        #   resume-mode turns without raising peak RSS.
        sem_wait_start = time.time()
        _IN_FLIGHT_SEMAPHORE.acquire()
        sem_wait = time.time() - sem_wait_start

        try:
            if pooled is not None:
                return self._reuse_turn(
                    pooled,
                    source_session_id,
                    prompt,
                    timeout,
                    on_session_id,
                    sem_wait,
                )
            return self._fresh_spawn_turn(
                source_session_id,
                prompt,
                mode,
                effective_cwd,
                extra_env,
                timeout,
                on_session_id,
                sem_wait,
                preallocated_session_id=preallocated_session_id,
            )
        finally:
            _IN_FLIGHT_SEMAPHORE.release()

    # ---- private: top-level turn paths ----

    def _reuse_turn(
        self,
        pooled: _PooledProcess,
        source_session_id: str,
        prompt: str,
        timeout: int,
        on_session_id: Optional[Callable[[str], None]],
        sem_wait: float,
    ) -> TurnResult:
        """Run one turn on an existing pooled process. Evicts on failure."""
        self._log_sem_wait(pooled.log, sem_wait)
        try:
            print(
                f"[callstack] reuse (session={source_session_id[:8]}..., cwd={pooled.cwd}, log={pooled.log_path})",
                file=sys.stderr,
            )
            with pooled.lock:
                return self._run_one_turn(
                    pooled,
                    prompt,
                    timeout,
                    on_session_id,
                    do_handshake=False,
                )
        except Exception:
            # Any failure on a pooled process leaves it in an unknown state.
            _get_pool().evict(source_session_id)
            raise

    def _fresh_spawn_turn(
        self,
        source_session_id: str,
        prompt: str,
        mode: str,
        effective_cwd: str,
        extra_env: Optional[dict],
        timeout: int,
        on_session_id: Optional[Callable[[str], None]],
        sem_wait: float,
        *,
        preallocated_session_id: Optional[str] = None,
    ) -> TurnResult:
        """Spawn a new claude subprocess, run one turn, and pool it on success.

        Holds `_SPAWN_SEMAPHORE` for the full first turn — that's the phase
        where the child is bootstrapping and accumulating its ~0.5–2 GB RSS,
        which the spawn cap is designed to bound. The semaphore is released
        before the call returns; subsequent reuse turns on the pooled
        process never reacquire it (PERF-H)."""
        spawn_wait_start = time.time()
        _SPAWN_SEMAPHORE.acquire()
        sem_wait += time.time() - spawn_wait_start
        try:
            try:
                pooled = self._spawn(
                    source_session_id,
                    mode,
                    effective_cwd,
                    extra_env,
                    preallocated_session_id=preallocated_session_id,
                )
            except Exception as e:
                raise RuntimeError(f"Failed to start claude CLI: {e}") from e

            self._log_sem_wait(pooled.log, sem_wait)
            try:
                with pooled.lock:
                    result = self._run_one_turn(
                        pooled,
                        prompt,
                        timeout,
                        on_session_id,
                        do_handshake=True,
                        preallocated_session_id=preallocated_session_id,
                    )
            except Exception:
                pooled.close()
                raise

            # Pool keyed by the session id the CLI reported. For resume mode
            # that equals source_session_id; for fork/fresh it's a brand-new id.
            _get_pool().register(result.session_id, pooled)
            return result
        finally:
            _SPAWN_SEMAPHORE.release()

    @staticmethod
    def _log_sem_wait(log, sem_wait: float) -> None:
        if sem_wait <= 0.5:
            return
        try:
            log.write(f"semaphore-wait: {sem_wait:.2f}s (cap={_MAX_CONCURRENT_FORKS})\n")
            log.flush()
        except (OSError, ValueError):
            pass

    # ---- private: spawn ----

    def _spawn(
        self,
        source_session_id: str,
        mode: str,
        effective_cwd: str,
        extra_env: Optional[dict],
        *,
        preallocated_session_id: Optional[str] = None,
    ) -> _PooledProcess:
        cmd = self._build_cmd(source_session_id, mode, preallocated_session_id=preallocated_session_id)
        env = {**os.environ, **self._env_extra, **(extra_env or {})}
        # Stamp the child's own session UUID into its env so its MCP
        # server resolves itself deterministically without depending on
        # Claude Code's env-propagation behavior. Pre-allocated for
        # fork/fresh (the same UUID is passed to claude via --session-id);
        # equal to the resumed id for resume mode.
        own_session = preallocated_session_id if mode in ("fork", "fresh") else source_session_id
        if own_session:
            env[_env.ENV_OWN_SESSION] = own_session
        stem = (preallocated_session_id or source_session_id or "")[:8] or "fresh"
        log_path = _process_log_path(stem)
        # PERF-I: line-buffered (`buffering=1`) means a single newline flush
        # per write, removing the explicit log.flush() most callers do per
        # stderr/event line. We keep explicit flushes around critical writes
        # (timeout, exit code) for paranoia.
        log = open(log_path, "w", buffering=1)
        log.write(f"cmd: {' '.join(cmd)}\ncwd: {effective_cwd}\n")
        log.flush()
        print(
            f"[callstack] spawn (mode={mode}, source={stem}..., cwd={effective_cwd}, log={log_path})",
            file=sys.stderr,
        )

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=effective_cwd,
            env=env,
        )
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        entry = _PooledProcess(
            proc=proc,
            stdin=proc.stdin,
            stdout=proc.stdout,
            log=log,
            log_path=log_path,
            cwd=effective_cwd,
        )
        # Drain stderr for the LIFETIME of the process — survives across
        # multiple pooled turns, so we don't restart on each reuse.
        # PERF-I: with the log opened line-buffered above, each
        # log.write that ends in '\n' flushes on its own. SEC-005: cap
        # the total stderr bytes appended per process to defend against
        # a malfunctioning child filling the disk.
        stderr = proc.stderr
        bytes_written = [0]
        capped = [False]

        def _drain():
            for line in stderr:
                if capped[0]:
                    continue
                try:
                    n = len(line)
                    if bytes_written[0] + n > _STDERR_LOG_CAP:
                        log.write(f"STDERR: ...stderr log capped at {_STDERR_LOG_CAP} bytes; further lines discarded\n")
                        capped[0] = True
                        continue
                    log.write(f"STDERR: {line}")
                    bytes_written[0] += n
                except (OSError, ValueError):
                    return

        threading.Thread(target=_drain, daemon=True).start()
        return entry

    # ---- private: per-turn I/O ----

    def _run_one_turn(
        self,
        entry: _PooledProcess,
        prompt: str,
        timeout: int,
        on_session_id: Optional[Callable[[str], None]],
        *,
        do_handshake: bool,
        preallocated_session_id: Optional[str] = None,
    ) -> TurnResult:
        """Send one user message + read until result. Per-turn watchdog only
        kills the process if THIS turn exceeds `timeout`; otherwise the
        process stays alive for pool reuse."""
        start = time.time()
        cancel = threading.Event()
        timed_out = threading.Event()

        def _watchdog():
            if not cancel.wait(timeout):
                timed_out.set()
                try:
                    entry.log.write(f"TIMEOUT after {timeout}s\n")
                    entry.log.flush()
                except (OSError, ValueError):
                    pass
                try:
                    entry.proc.kill()
                except OSError:
                    pass

        threading.Thread(target=_watchdog, daemon=True).start()

        text_parts: list[str] = []
        result_meta: dict = {}
        session_id: Optional[str] = None
        try:
            if do_handshake and not entry.initialized:
                self._handshake(entry.stdin, entry.log)
                entry.initialized = True
            self._send_user_message(entry.stdin, prompt, entry.log)
            session_id = self._read_until_result(
                entry.stdin,
                entry.stdout,
                text_parts,
                entry.log,
                result_meta,
                on_session_id=on_session_id,
            )
        finally:
            cancel.set()

        text = "".join(text_parts)
        if timed_out.is_set():
            raise TurnTimeout(f"turn timed out after {timeout}s", partial=text)
        if not session_id:
            raise RuntimeError(
                f"claude CLI exited without reporting a session id "
                f"(returncode={entry.proc.returncode}, log={entry.log_path})"
            )
        # Subprocess exit-code check. `returncode is None` means the process
        # is still alive (the normal happy path — we pool it for reuse).
        # A non-None, non-zero rc means claude exited with an error after
        # emitting `result`; with no text to parse there's nothing the
        # driver can do, so raise here. If text is present, the driver's
        # envelope parser will decide whether the turn succeeded — we just
        # log a warning so the failure is visible in the per-process log.
        rc = entry.proc.returncode
        if rc is not None and rc != 0:
            if not text:
                raise RuntimeError(f"claude CLI exited with returncode={rc} and no output (log={entry.log_path})")
            try:
                entry.log.write(
                    f"WARN: claude CLI exited with returncode={rc} after "
                    f"emitting output; envelope parsing will decide turn outcome\n"
                )
                entry.log.flush()
            except (OSError, ValueError):
                pass

        # Consistency check: when we pre-allocated a UUID and passed it
        # to claude via `--session-id`, the session id reported on the
        # wire MUST match. A mismatch means a future Claude Code
        # changed --session-id semantics (silently ignored, rewritten,
        # de-duplicated, …) — every downstream locate() in this
        # subtree would resolve a UUID that doesn't match what
        # CALLSTACK_OWN_SESSION says, cross-forking siblings. Fail
        # loud here rather than write a corrupted report.
        if preallocated_session_id is not None and session_id != preallocated_session_id:
            raise RuntimeError(
                f"claude reported session_id {session_id} but callstack "
                f"pre-allocated {preallocated_session_id} via "
                f"--session-id. Refusing to continue: Claude Code's "
                f"--session-id contract may have changed; nested "
                f"/calls in this subtree would cross-fork."
            )

        entry.session_id = session_id
        entry.last_used = time.monotonic()
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

    # ---- private: cmd + protocol ----

    def _build_cmd(
        self,
        source_session_id: str,
        mode: str,
        *,
        preallocated_session_id: Optional[str] = None,
    ) -> list[str]:
        cmd = [
            "claude",
            "--output-format",
            "stream-json",
            "--input-format",
            "stream-json",
            "--verbose",
            "--permission-prompt-tool",
            "stdio",
            "--permission-mode",
            self._permission_mode,
        ]
        if mode == "fork":
            cmd.extend(["--resume", source_session_id, "--fork-session"])
        elif mode == "resume":
            cmd.extend(["--resume", source_session_id])
        # mode == "fresh": no --resume, no --fork-session — brand-new session.
        # Pin the new session's UUID for fork/fresh so the child's MCP
        # server can read it back from CALLSTACK_OWN_SESSION env without
        # mtime guessing. Verified empirically that `--session-id`
        # composes with `--resume + --fork-session` on Claude Code
        # 2026 (test in commit msg).
        if mode in ("fork", "fresh") and preallocated_session_id and _UUID_RE.fullmatch(preallocated_session_id):
            cmd.extend(["--session-id", preallocated_session_id])
        if self._model:
            cmd.extend(["--model", self._model])
        return cmd

    @staticmethod
    def _send(stdin, obj: dict) -> None:
        stdin.write(json.dumps(obj) + "\n")
        stdin.flush()

    def _handshake(self, stdin, log) -> None:
        log.write("→ initialize\n")
        log.flush()
        self._send(
            stdin,
            {
                "type": "control_request",
                "request_id": f"req_init_{uuid.uuid4().hex[:8]}",
                "request": {"subtype": "initialize", "hooks": None},
            },
        )

    def _send_user_message(self, stdin, prompt: str, log) -> None:
        log.write(f"→ user message ({len(prompt)} chars)\n")
        log.flush()
        self._send(
            stdin,
            {
                "type": "user",
                "session_id": "",
                "message": {"role": "user", "content": prompt},
                "parent_tool_use_id": None,
            },
        )

    def _read_until_result(
        self,
        stdin,
        stdout,
        text_parts: list,
        log,
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
            # SEC-005: cap line size to bound peak memory under a malicious
            # or malfunctioning child that emits an unbounded line. Truncation
            # (no trailing newline + at-limit length) is treated as a
            # protocol error and ends the turn.
            raw = stdout.readline(_NDJSON_MAX_LINE)
            if not raw:
                log.write("← EOF\n")
                log.flush()
                return session_id
            if len(raw) == _NDJSON_MAX_LINE and not raw.endswith("\n"):
                log.write(f"← line exceeded {_NDJSON_MAX_LINE} bytes — protocol error; ending turn\n")
                log.flush()
                raise RuntimeError(f"claude stdout exceeded {_NDJSON_MAX_LINE}-byte NDJSON line cap; aborting turn")
            raw = raw.strip()
            if not raw:
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.write(f"← unparseable: {raw[:200]}\n")
                log.flush()
                continue

            mtype = msg.get("type")
            log.write(f"← {mtype}\n")
            log.flush()

            # Fire early-session-id callback the moment we see one on any
            # message type (system init carries it first; assistant messages
            # also include it in stream-json output). Passing `log` routes
            # exceptions to both stderr and the per-turn log (SEC-011); the
            # shared helper keeps this in lockstep with ScriptedChannel.
            if not early_id_fired and on_session_id is not None:
                early_sid = msg.get("session_id")
                if isinstance(early_sid, str) and early_sid:
                    early_id_fired = True
                    _fire_on_session_id(on_session_id, early_sid, log)

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
            tool_name = request.get("tool_name", "")
            # SEC-011: this is the ONE fail-closed swallow. If the user-
            # supplied permission_handler raises, we MUST NOT default-allow
            # — that would let a buggy handler turn into a silent
            # permissive policy. Send a deny + log + carry on.
            try:
                response = self._handler(tool_name, request.get("input", {}))
            except Exception as e:
                print(
                    f"[callstack] permission_handler raised on tool={tool_name!r}: "
                    f"{type(e).__name__}: {str(e)[:200]} — denying request",
                    file=sys.stderr,
                )
                response = {
                    "behavior": "deny",
                    "message": (f"permission_handler raised {type(e).__name__}; request denied"),
                }
        else:
            response = {}
        self._send(
            stdin,
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": request_id, "response": response},
            },
        )


# --------------------------------------------------------------------------
# Test channel — re-exported from `agent_callstack.testing` so the
# production surface here stays minimal. Existing imports of the form
# `from agent_callstack.channel import ScriptedChannel` keep working.
# --------------------------------------------------------------------------

from .testing import (  # noqa: E402
    ScriptedChannel,
    ScriptedEntry,
    ScriptedResponse,
)

__all__ = [
    "Channel",
    "ClaudeChannel",
    "TurnResult",
    "TurnTimeout",
    "PermissionHandler",
    "allow_all",
    "shutdown_pool",
    "ScriptedChannel",
    "ScriptedEntry",
    "ScriptedResponse",
]
