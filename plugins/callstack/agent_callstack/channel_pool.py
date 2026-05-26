"""Process-per-session pool (PERF-D) for the Claude CLI channel.

Each turn used to spawn a fresh `claude` subprocess (~0.5–2 GB RSS, ~1 s
cold start). A typical node runs 3–6 turns (initial fork → CALL → child
returned → resume → final return), so naive spawn-per-turn paid that
cost 3–6× per node. The pool keeps one long-lived `claude` process per
session_id and reuses it across that session's consecutive resume-mode
turns:

  * mode='fork' / mode='fresh' — always spawn (creates a NEW session id).
    After the turn, register the live process under the new id.
  * mode='resume' — look up source_session_id in the pool. Hit: reuse
    the existing stdin/stdout. Miss: spawn `claude --resume <id>` and
    register under source_session_id.

The CLI's stream-json input mode strips slash commands (verified
2026-05), so a single process cannot be multiplexed across different
session ids via `/resume`. The pool is therefore strictly
process-PER-session, not a shared multiplex.

Eviction: LRU when pool size exceeds CALLSTACK_MAX_CONCURRENT_FORKS.
In-use pooled processes (per-process lock held) are skipped during
eviction — the pool may exceed cap briefly until a turn finishes.

The pool is torn down at interpreter exit via `atexit`, or explicitly
via `Caller.close()` / `shutdown_pool()`. `channel.py` owns the spawn /
in-flight semaphores that bound concurrency; this module owns the
process registry and its lifecycle.
"""

from __future__ import annotations

import atexit
import subprocess
import threading
import time
from typing import Callable, Optional

from . import env as _env

# Default pool size: the cold-start concurrency cap. `channel.py` imports
# this to size `_SPAWN_SEMAPHORE` so the pool and the spawn cap stay in
# lockstep on the same env knob (CALLSTACK_MAX_CONCURRENT_FORKS).
_MAX_CONCURRENT_FORKS = _env.max_concurrent_forks()


class _PooledProcess:
    """A long-lived `claude` subprocess bound to one session_id.

    Pooled by session_id; reused across that session's consecutive
    resume-mode turns. The per-process `lock` serializes stdin writes
    when multiple threads happen to drive the same session (rare; a
    yield + concurrent resume would do it).

    `initialized` tracks whether the stream-json `initialize` handshake
    has been sent — required once per process, never re-sent on reuse.
    """

    def __init__(self, proc: subprocess.Popen, stdin, stdout, log, log_path: str, cwd: str):
        self.proc = proc
        self.stdin = stdin
        self.stdout = stdout
        self.log = log
        self.log_path = log_path
        self.cwd = cwd
        self.session_id: Optional[str] = None
        self.initialized = False
        self.last_used = time.monotonic()
        self.lock = threading.Lock()
        self.closed = False

    def is_alive(self) -> bool:
        if self.closed:
            return False
        return self.proc.poll() is None

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.stdin.close()
        except OSError:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            try:
                self.proc.kill()
                self.proc.wait()
            except OSError:
                pass
        try:
            self.log.write(f"Process exited with code {self.proc.returncode}\n")
            self.log.close()
        except (OSError, ValueError):
            pass


class ClaudePool:
    """Thread-safe LRU pool of `_PooledProcess` keyed by session_id.

    `clock` is the monotonic time source stamped onto `last_used` for LRU
    ordering; injectable so tests can drive eviction order deterministically
    instead of relying on real sub-millisecond sleeps to separate timestamps."""

    def __init__(self, max_size: int, clock: Callable[[], float] = time.monotonic):
        self._max_size = max_size
        self._processes: dict[str, _PooledProcess] = {}
        self._lock = threading.Lock()
        self._clock = clock

    @property
    def max_size(self) -> int:
        return self._max_size

    def acquire(self, session_id: str) -> Optional[_PooledProcess]:
        """Look up a pooled process by session_id. Returns the entry (with
        `last_used` updated) or None if absent / dead. Caller is responsible
        for acquiring `entry.lock` before driving I/O."""
        with self._lock:
            entry = self._processes.get(session_id)
            if entry is None:
                return None
            if not entry.is_alive():
                del self._processes[session_id]
                # Close outside the pool lock.
                dead = entry
            else:
                entry.last_used = self._clock()
                return entry
        dead.close()
        return None

    def register(self, session_id: str, entry: _PooledProcess) -> None:
        """Insert `entry` under `session_id`. Evicts LRU idle entries to
        respect `max_size`. Replaces any existing entry under the same id.

        The just-registered entry is protected from immediate eviction —
        otherwise a register-when-full would silently drop the new entry."""
        to_close: list[_PooledProcess] = []
        with self._lock:
            entry.session_id = session_id
            entry.last_used = self._clock()
            old = self._processes.pop(session_id, None)
            if old is not None and old is not entry:
                to_close.append(old)
            self._processes[session_id] = entry
            to_close.extend(self._evict_excess_locked(protect=session_id))
        for e in to_close:
            e.close()

    def evict(self, session_id: str) -> None:
        with self._lock:
            entry = self._processes.pop(session_id, None)
        if entry is not None:
            entry.close()

    def size(self) -> int:
        with self._lock:
            return len(self._processes)

    def session_ids(self) -> list[str]:
        with self._lock:
            return list(self._processes.keys())

    def shutdown(self) -> None:
        """Close every pooled process. Safe to call repeatedly."""
        with self._lock:
            entries = list(self._processes.values())
            self._processes.clear()
        for e in entries:
            e.close()

    # ---- private ----

    def _evict_excess_locked(
        self,
        *,
        protect: Optional[str] = None,
    ) -> list[_PooledProcess]:
        """Pop entries beyond `max_size`, preferring idle (lock-free) ones
        and lowest `last_used` first. Caller holds `self._lock`. Returns
        entries the caller must `close()` outside the lock.

        `protect` is a session_id whose entry will not be selected even if
        it is the least-recently-used — used by register() to guarantee
        the just-added process survives at least until another turn
        bumps an older entry.

        PERF-105: sorts the pool ONCE per call instead of once per evicted
        entry. The previous implementation's `sorted()` inside the while
        loop was O((N-max+1) × N log N) — fine for the default cap of 8,
        bad if pool size grows. Now O(N log N) total."""
        to_close: list[_PooledProcess] = []
        if len(self._processes) <= self._max_size:
            return to_close
        # One-shot LRU ordering: oldest `last_used` first.
        ranked = sorted(self._processes.items(), key=lambda kv: kv[1].last_used)
        for k, v in ranked:
            if len(self._processes) <= self._max_size:
                break
            if k == protect:
                continue
            # Try-acquire; release immediately. Skipping in-use entries
            # avoids tearing down a process mid-turn.
            if not v.lock.acquire(blocking=False):
                continue
            v.lock.release()
            entry = self._processes.pop(k, None)
            if entry is not None:
                to_close.append(entry)
        # If we couldn't reach `max_size` (every remaining entry is busy or
        # protected), accept the temporary overage — the next register()
        # after a turn finishes will catch up.
        return to_close


_pool: Optional[ClaudePool] = None
_pool_init_lock = threading.Lock()


def _get_pool() -> ClaudePool:
    """Return the module-level process pool, creating it on first use.

    Lazy so tests can monkeypatch / replace `_pool` before any spawn
    happens. The atexit hook is registered on first creation."""
    global _pool
    if _pool is None:
        with _pool_init_lock:
            if _pool is None:
                _pool = ClaudePool(_MAX_CONCURRENT_FORKS)
                atexit.register(_pool.shutdown)
    return _pool


def shutdown_pool() -> None:
    """Tear down every pooled `claude` subprocess. Called by `Caller.close()`
    and at interpreter exit. `_pool` is not nulled, so subsequent run_turn
    calls reuse the same (now-empty) pool object, repopulating it on demand."""
    global _pool
    if _pool is not None:
        _pool.shutdown()
