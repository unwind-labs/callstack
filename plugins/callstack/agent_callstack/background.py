"""Background-run lifecycle — one public boundary over the asyncio bookkeeping.

`BackgroundRuns` owns everything an async host (the MCP server) needs to launch
a `Caller.call_many` off the event loop and reconcile it later: the registry of
in-flight tasks, the concurrency cap, the reaper for finished-but-unreconciled
runs, the `asyncio.shield`-on-await, and force-finalizing a crashed run's frames
so the parent never sees a stuck spinner.

It returns typed *outcomes* (`Started`, `CapReached`, `Pending`, `Done`,
`Crashed`, `NotFound`) and never touches the JSON wire format — that stays in
the adapter (`mcp_server.py`). The split keeps the lifecycle independently
testable without spinning up FastMCP or parsing envelopes.

This is the package's async adapter; the synchronous core (Caller, Driver,
channel) stays asyncio-free.
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol, Sequence, Union

from .env import max_background
from .report import InvocationReport
from .results import MultiResult


class _CallerLike(Protocol):
    """Structural type for the thing `BackgroundRuns` schedules. `Caller`
    satisfies it; tests pass a stub. Keeping it structural decouples this
    module from the package root (where `Caller` lives) and avoids a circular
    import."""

    def call_many(self, tasks: Sequence[str], *,
                  context: str = "fork") -> MultiResult: ...


# ---------- start() outcomes ----------

@dataclass(frozen=True)
class Started:
    """The run was scheduled and parked in the registry."""
    invoke_id: str
    report_path: str


@dataclass(frozen=True)
class CapReached:
    """The registry is full; the run was NOT scheduled. `outstanding` runs are
    parked against a cap of `cap`."""
    cap: int
    outstanding: int


StartOutcome = Union[Started, CapReached]


# ---------- reconcile() outcomes ----------

@dataclass(frozen=True)
class Pending:
    """Still running after the await budget elapsed. The entry is kept so the
    caller can poll again."""
    invoke_id: str
    report_path: str


@dataclass(frozen=True)
class Done:
    """The run finished; `result` is its `MultiResult`. The entry was popped."""
    invoke_id: str
    report_path: str
    result: MultiResult


@dataclass(frozen=True)
class Crashed:
    """`call_many` raised (an internal failure, not a per-task `CallFailed`).
    The entry was popped and the run's own frames force-finalized. `error` is a
    pre-formatted ``Type: message`` string."""
    invoke_id: str
    report_path: str
    error: str


@dataclass(frozen=True)
class NotFound:
    """No run with this `invoke_id` is (or ever was) in the registry."""
    invoke_id: str


ReconcileOutcome = Union[Pending, Done, Crashed, NotFound]


@dataclass
class _Run:
    task: "asyncio.Task[MultiResult]"
    report_path: str
    log_dir: Path
    invoke_id: str
    # Set True once `reconcile` has returned `Pending` for this run, i.e. a
    # caller is actively polling it. A polled run's result is pinned in the
    # registry until the next `reconcile` delivers it — `reap` must never drop
    # it (doing so would strand the `MultiResult`). Distinguishes an
    # awaited-will-poll-again run from a fire-and-forget one that was never
    # reconciled.
    polled: bool = False


class BackgroundRuns:
    """Registry of in-flight `Caller.call_many` invocations launched on an
    asyncio event loop.

    One instance is shared by an async host across many tool calls (FastMCP
    serves every call on one loop in one process, so a task scheduled in
    `start` keeps running while later calls — including the matching
    `reconcile` — are served).
    """

    def __init__(self, *, max_outstanding: Optional[Callable[[], int]] = None):
        # `max_outstanding` is read *per start* (not cached) so a host that
        # changes `CALLSTACK_MAX_BACKGROUND` — or a test that monkeypatches it
        # per-case — sees the current value. Defaults to the package env policy.
        self._runs: dict[str, _Run] = {}
        self._max_outstanding = max_outstanding or max_background

    def start(self, *, invoke_id: str, caller: _CallerLike,
              tasks: Sequence[str], context: str,
              report_path: str, log_dir: Path) -> StartOutcome:
        """Schedule `caller.call_many(tasks, context=...)` on a worker thread
        and park it under `invoke_id`. First reaps abandoned fire-and-forget
        runs (so a host that fires fast background calls and never awaits them
        doesn't trip the cap), then enforces the cap against *in-flight* runs
        only. Returns `Started` on success or `CapReached` when the cap is hit.

        The cap counts in-flight (not-yet-finished) runs rather than all parked
        entries: a finished run holds only a cheap `MultiResult`, not the
        ~0.5–2 GB `claude` subprocess the cap exists to bound. Counting
        finished-undelivered runs against the cap would force `reap` to drop
        them to free slots — and that is exactly what would strand a polled
        run's result (see `_Run.polled` / `reap`).

        Must be called with a running event loop (it creates a task)."""
        self.reap()
        cap = self._max_outstanding()
        inflight = self._inflight()
        if inflight >= cap:
            return CapReached(cap=cap, outstanding=inflight)
        task: "asyncio.Task[MultiResult]" = asyncio.create_task(
            asyncio.to_thread(caller.call_many, list(tasks), context=context)
        )
        self._runs[invoke_id] = _Run(
            task=task, report_path=report_path,
            log_dir=Path(log_dir), invoke_id=invoke_id,
        )
        return Started(invoke_id=invoke_id, report_path=report_path)

    async def reconcile(self, invoke_id: str, *,
                        timeout: float) -> ReconcileOutcome:
        """Await the run up to `timeout` seconds.

        - unknown id            -> `NotFound`
        - still running         -> `Pending` (entry kept; poll again)
        - finished cleanly      -> `Done` (entry popped)
        - `call_many` raised    -> `Crashed` (entry popped, own frames finalized)

        The await is `shield`ed so a timeout cancels only the wait, never the
        underlying `call_many` — the run keeps going and can be polled again."""
        run = self._runs.get(invoke_id)
        if run is None:
            return NotFound(invoke_id)
        try:
            multi: MultiResult = await asyncio.wait_for(
                asyncio.shield(run.task), timeout=timeout,
            )
        except asyncio.TimeoutError:
            # Healthy and still running — keep the entry, do NOT finalize.
            # Pin it: a caller is now polling, so even if the task finishes
            # before the next reconcile, `reap` must not drop its result.
            run.polled = True
            return Pending(invoke_id, run.report_path)
        except Exception as e:
            self._runs.pop(invoke_id, None)
            # The run's reporter.finalize may not have run before the
            # exception propagated; force-terminate any surviving
            # non-terminal frames so the parent sees a clean error rather
            # than a stuck-running canvas row.
            self._finalize_crashed(
                run,
                reason="background call_many raised before terminal frame state",
            )
            return Crashed(invoke_id, run.report_path,
                           f"{type(e).__name__}: {e}")
        self._runs.pop(invoke_id, None)
        return Done(invoke_id, run.report_path, multi)

    def reap(self) -> None:
        """Drop finished runs that were never polled — i.e. fire-and-forget
        calls a host started and then abandoned. Their results are unreachable
        (no caller is listening) so they shouldn't linger in the registry.

        A `reconcile` that returned `Pending` pins the entry (`_Run.polled`),
        so its result survives here until the next `reconcile` delivers it.
        Only `done() and not polled` entries are truly abandoned. Any pending
        exception on a reaped task is consumed so asyncio doesn't warn at GC."""
        stale = [iid for iid, r in self._runs.items()
                 if r.task.done() and not r.polled]
        for iid in stale:
            run = self._runs.pop(iid)
            if not run.task.cancelled():
                run.task.exception()  # consume so asyncio stays quiet

    def _inflight(self) -> int:
        """Count of runs whose task has not yet finished. This is what the
        cap bounds — a finished-but-undelivered run holds only its result, not
        a live subprocess."""
        return sum(1 for r in self._runs.values() if not r.task.done())

    def task_for(self, invoke_id: str) -> "Optional[asyncio.Task[MultiResult]]":
        """The asyncio.Task backing a parked run, or None. Lets an advanced
        host integrate with its own loop (and lets tests await a run to
        completion deterministically before reconciling)."""
        run = self._runs.get(invoke_id)
        return run.task if run is not None else None

    def clear(self) -> None:
        """Forget all entries without cancelling their tasks. Intended for test
        teardown; in production runs are removed by `reconcile`/`reap`."""
        self._runs.clear()

    def __contains__(self, invoke_id: str) -> bool:
        return invoke_id in self._runs

    def __len__(self) -> int:
        return len(self._runs)

    # ---- internal ----

    def _finalize_crashed(self, run: _Run, *, reason: str) -> None:
        """Best-effort post-mortem for a crashed run: force-terminate any
        non-terminal frames it owns via the public report facade. Never raises
        — logs to stderr on failure (the run already crashed; this is cleanup)."""
        try:
            report = InvocationReport(
                invoke_id=run.invoke_id, log_dir=run.log_dir, cwd="",
            )
            report.finalize_own_frames(reason=reason)
        except Exception as e:  # pragma: no cover - defensive
            print(f"[callstack] WARN finalize_own_frames raised reconciling "
                  f"background run {run.invoke_id} ({reason}): "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
