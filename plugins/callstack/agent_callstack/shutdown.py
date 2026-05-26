"""Process-wide shutdown hardening for live reporters.

When a process holding `_LiveReporter` instances is torn down — atexit,
SIGTERM, SIGINT — any non-terminal nodes in the in-memory trees would
otherwise be left at ``awaiting_*`` on disk, and the merged report would
render an in-progress spinner forever. This module owns the registry
that lets each live reporter write a post-mortem frame at shutdown.

Why a dedicated module (REVIEW-202)
-----------------------------------
The registry, atexit, and signal-handler install used to live inside
`_LiveReporter`, triggered as a side effect of construction. That had
two problems:

1. ``signal.signal()`` must run on the main thread. ``_LiveReporter`` is
   constructed from `Caller.call_many` which the MCP server invokes via
   ``asyncio.to_thread`` — i.e. always on a *worker* thread. The
   constructor-side install silently skipped, so signal handlers were
   never registered in the very scenario the fix was meant to address.

2. Construction had global, irreversible side effects — making the
   class harder to test in isolation.

The new shape: this module is a pure registry. The install entry point
is `install_shutdown_hooks()`, called once at process startup from the
main thread (today: at `agent_callstack/__init__.py` module-load time).
Reporters just `register_reporter` / `unregister_reporter`. The handler
install is idempotent and silently no-ops on subsequent calls.

Anything with an ``_emergency_finalize_on_shutdown()`` method can
register — the registry is type-agnostic.
"""
from __future__ import annotations

import atexit
import os
import signal
import threading
from typing import Any, Protocol


class _ShutdownFlushable(Protocol):
    """Duck-type contract for everything in the registry. ``_LiveReporter``
    is the only implementor today; the Protocol exists so the registry
    isn't coupled to the reporter module."""

    def _emergency_finalize_on_shutdown(self) -> None: ...


_ACTIVE_REPORTERS: "set[_ShutdownFlushable]" = set()
# RLock, not Lock (M-A1): the SIGTERM/SIGINT handler installed by
# `_chain_signal_handler` calls `flush_active_reporters`, which acquires
# this lock. Signals are delivered on the main thread between bytecodes,
# so one can land while the main thread already holds the lock inside
# `register_reporter` / `unregister_reporter` / `flush_active_reporters`.
# With a plain Lock that re-entry self-deadlocks the process during
# shutdown — exactly when finalization must succeed. RLock permits the
# same-thread re-acquire; the critical sections are short and never block
# on other locks, so recursive entry stays bounded.
_ACTIVE_REPORTERS_LOCK = threading.RLock()
_HOOKS_INSTALLED = False
_INSTALL_LOCK = threading.Lock()


def register_reporter(reporter: _ShutdownFlushable) -> None:
    """Add `reporter` to the shutdown registry. Idempotent.

    Does NOT install hooks — that's a one-time process-startup concern
    handled by `install_shutdown_hooks()`. Calling this from a worker
    thread is safe (the registry mutation is lock-guarded)."""
    with _ACTIVE_REPORTERS_LOCK:
        _ACTIVE_REPORTERS.add(reporter)


def unregister_reporter(reporter: _ShutdownFlushable) -> None:
    """Drop `reporter` from the shutdown registry. Idempotent."""
    with _ACTIVE_REPORTERS_LOCK:
        _ACTIVE_REPORTERS.discard(reporter)


def flush_active_reporters() -> None:
    """Walk the registry and let each reporter write a post-mortem
    frame for whatever non-terminal nodes it still holds.

    Best-effort: an exception from one reporter must not block siblings
    from getting their shot at finalization, and the whole routine must
    return promptly because the process is on its way down.

    Snapshot the set before iterating: ``_emergency_finalize_on_shutdown``
    does NOT unregister, so we copy under the lock and then walk the copy.
    Even though the registry lock is now an RLock (M-A1) and a re-entrant
    call from the same thread *could* mutate the set, snapshotting keeps
    the walk stable regardless of re-entry."""
    with _ACTIVE_REPORTERS_LOCK:
        snapshot = list(_ACTIVE_REPORTERS)
    for reporter in snapshot:
        try:
            reporter._emergency_finalize_on_shutdown()
        except Exception:
            # Last-resort defensive: stderr may be closed during atexit.
            continue


def install_shutdown_hooks() -> bool:
    """Install the atexit + SIGTERM/SIGINT handlers exactly once per
    process. Returns True iff signal handlers were installed (False when
    called from a worker thread or in a sandbox that forbids
    ``signal.signal``).

    Idempotent. Safe to call from any thread, but only the first
    main-thread invocation actually wires signal handlers — subsequent
    calls just return. Call this from the main thread at process startup
    (today: at `agent_callstack` module-load time)."""
    global _HOOKS_INSTALLED
    with _INSTALL_LOCK:
        if _HOOKS_INSTALLED:
            return False
        atexit.register(flush_active_reporters)
        _HOOKS_INSTALLED = True
        # `signal.signal` must run on the main thread.
        if threading.current_thread() is not threading.main_thread():
            return False
        installed_any = False
        for sig in (signal.SIGTERM, signal.SIGINT):
            if _chain_signal_handler(sig):
                installed_any = True
        return installed_any


def _chain_signal_handler(sig: int) -> bool:
    """Install a SIGTERM/SIGINT handler that runs
    :func:`flush_active_reporters` then chains to whatever handler was
    previously registered. Chaining preserves caller-installed behaviours
    like Python's KeyboardInterrupt for SIGINT. Returns True iff the
    install actually happened."""
    try:
        prev = signal.getsignal(sig)
    except (ValueError, OSError):
        return False

    def handler(signum: int, frame: Any,
                _prev: Any = prev, _sig: int = sig) -> None:
        try:
            flush_active_reporters()
        finally:
            if callable(_prev):
                _prev(signum, frame)
            elif _prev == signal.SIG_DFL:
                # Restore default disposition and re-raise so the
                # process terminates with the original semantics.
                signal.signal(_sig, signal.SIG_DFL)
                os.kill(os.getpid(), _sig)
            # SIG_IGN: leave the signal ignored, matching prior behaviour.

    try:
        signal.signal(sig, handler)
        return True
    except (ValueError, OSError):
        # Not permitted (e.g. running inside a thread, or signal not
        # supported on this platform) — leave the prior handler in place.
        return False


def _reset_for_tests() -> None:
    """Test hook: clear the registry and reset the install flag so a
    fresh import-time install can be exercised. Never used in production."""
    global _HOOKS_INSTALLED
    with _ACTIVE_REPORTERS_LOCK:
        _ACTIVE_REPORTERS.clear()
    with _INSTALL_LOCK:
        _HOOKS_INSTALLED = False
