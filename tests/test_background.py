"""Boundary tests for `agent_callstack.BackgroundRuns`.

These exercise the background-run lifecycle directly — no FastMCP, no JSON
envelopes — through the public typed outcomes (`Started`, `CapReached`,
`Pending`, `Done`, `Crashed`, `NotFound`). They replace the parts of
`test_mcp_server.py` that used to reach into the module-level `_background_tasks`
dict to assert lifecycle behavior: the lifecycle now lives behind one type and
is verified at that type's boundary.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "callstack"
if str(_PLUGIN) not in sys.path:
    sys.path.insert(0, str(_PLUGIN))

from agent_callstack import (  # type: ignore  # noqa: E402
    BackgroundRuns, CapReached, Crashed, Done, MultiResult, NotFound,
    Pending, Result, Started,
)


def _ok(value: str = "ok") -> Result:
    return Result(value=value, summary=None, next=None,
                  duration=0.01, log=None, log_start=0)


class _StubCaller:
    """A `_CallerLike` that returns canned results, optionally blocking on a
    gate so a test can hold `call_many` open to observe the Pending branch."""

    def __init__(self, *, results: list, gate: threading.Event | None = None):
        self._results = results
        self._gate = gate

    def call_many(self, tasks, *, context: str = "fork") -> MultiResult:
        if self._gate is not None:
            self._gate.wait(timeout=5.0)
        return MultiResult(results=self._results)


class _BoomCaller:
    def call_many(self, tasks, *, context: str = "fork") -> MultiResult:
        raise RuntimeError("simulated internal failure")


def _start(runs: BackgroundRuns, caller, *, invoke_id="iid",
           report_path="/tmp/r.yaml", log_dir=None):
    return runs.start(
        invoke_id=invoke_id, caller=caller, tasks=["x"], context="fork",
        report_path=report_path, log_dir=Path(log_dir or "/tmp"),
    )


@pytest.mark.asyncio
async def test_start_returns_started_and_parks_run():
    runs = BackgroundRuns()
    gate = threading.Event()
    outcome = _start(runs, _StubCaller(results=[_ok()], gate=gate))
    assert isinstance(outcome, Started)
    assert outcome.invoke_id == "iid"
    assert outcome.report_path == "/tmp/r.yaml"
    # Parked until reconciled.
    assert "iid" in runs
    assert len(runs) == 1
    # Drain.
    gate.set()
    await runs.reconcile("iid", timeout=5)


@pytest.mark.asyncio
async def test_reconcile_done_pops_and_carries_result():
    runs = BackgroundRuns()
    _start(runs, _StubCaller(results=[_ok("done")]))
    outcome = await runs.reconcile("iid", timeout=5)
    assert isinstance(outcome, Done)
    assert outcome.result.results[0].value == "done"
    # Reconciled entries are popped so memory doesn't grow unbounded.
    assert "iid" not in runs


@pytest.mark.asyncio
async def test_reconcile_timeout_returns_pending_and_keeps_entry():
    runs = BackgroundRuns()
    gate = threading.Event()
    _start(runs, _StubCaller(results=[_ok()], gate=gate))
    outcome = await runs.reconcile("iid", timeout=0.05)
    assert isinstance(outcome, Pending)
    assert outcome.report_path == "/tmp/r.yaml"
    # Pending keeps the entry so the caller can poll again — and must NOT
    # cancel the underlying work.
    assert "iid" in runs
    task = runs.task_for("iid")
    assert task is not None and not task.cancelled()
    # Drain.
    gate.set()
    assert isinstance(await runs.reconcile("iid", timeout=5), Done)


@pytest.mark.asyncio
async def test_reconcile_unknown_id_returns_not_found():
    runs = BackgroundRuns()
    outcome = await runs.reconcile("nope", timeout=1)
    assert isinstance(outcome, NotFound)
    assert outcome.invoke_id == "nope"


@pytest.mark.asyncio
async def test_reconcile_crash_pops_and_finalizes_frames(monkeypatch):
    runs = BackgroundRuns()
    finalized: list = []
    # Capture the boundary finalize so we prove a crashed run force-terminates
    # its own frames (so the parent never sees a stuck spinner) — without
    # needing real frames on disk.
    import agent_callstack.background as bg

    class _FakeReport:
        def __init__(self, *, invoke_id, log_dir, cwd):
            finalized.append((invoke_id, str(log_dir), cwd))

        def finalize_own_frames(self, *, reason):
            finalized.append(reason)
            return True

    monkeypatch.setattr(bg, "InvocationReport", _FakeReport)

    _start(runs, _BoomCaller(), log_dir="/tmp/inv")
    outcome = await runs.reconcile("iid", timeout=5)
    assert isinstance(outcome, Crashed)
    assert "simulated internal failure" in outcome.error
    assert outcome.error.startswith("RuntimeError:")
    assert "iid" not in runs
    # Frames were finalized for the crashed run.
    assert ("iid", "/tmp/inv", "") in finalized
    assert any(isinstance(x, str) and "terminal frame state" in x
               for x in finalized)


@pytest.mark.asyncio
async def test_cap_rejects_when_full():
    # Fixed cap via the injected provider — independent of env.
    runs = BackgroundRuns(max_outstanding=lambda: 2)
    gate = threading.Event()

    def caller():
        return _StubCaller(results=[_ok()], gate=gate)

    assert isinstance(_start(runs, caller(), invoke_id="a"), Started)
    assert isinstance(_start(runs, caller(), invoke_id="b"), Started)
    third = _start(runs, caller(), invoke_id="c")
    assert isinstance(third, CapReached)
    assert third.cap == 2
    assert third.outstanding == 2
    # The rejected run was NOT parked.
    assert "c" not in runs
    # Drain.
    gate.set()
    await runs.reconcile("a", timeout=5)
    await runs.reconcile("b", timeout=5)


@pytest.mark.asyncio
async def test_finished_unreconciled_runs_are_reaped_on_next_start():
    runs = BackgroundRuns(max_outstanding=lambda: 1)
    # First run completes immediately (no gate) and is never reconciled.
    _start(runs, _StubCaller(results=[_ok()]), invoke_id="a")
    task = runs.task_for("a")
    assert task is not None
    await task  # let it finish
    # Still nominally occupying the single slot until the next start reaps it.
    assert "a" in runs
    # A second start under cap=1 would trip the cap, but reap drops the
    # done-but-unreconciled entry first.
    second = _start(runs, _StubCaller(results=[_ok()]), invoke_id="b")
    assert isinstance(second, Started)
    assert "a" not in runs
    # Drain.
    await runs.reconcile("b", timeout=5)


@pytest.mark.asyncio
async def test_polled_pending_result_survives_a_sibling_start():
    """Regression for H1: a run that was reconciled-to-Pending must keep its
    result even if it finishes before the next poll AND an unrelated `start`
    runs in between. Earlier, `start`'s reaper dropped any finished entry —
    including a polled one whose caller was about to ask for it — stranding the
    `MultiResult` and turning the next `reconcile` into a spurious `NotFound`.

    Sequence: start A (gated) -> reconcile A (short) => Pending -> release &
    finish A -> start B -> reconcile A MUST be Done with A's original result."""
    runs = BackgroundRuns()
    gate = threading.Event()
    _start(runs, _StubCaller(results=[_ok("A-result")], gate=gate),
           invoke_id="a", report_path="/tmp/a.yaml")

    # Caller polls A while it's still running -> Pending pins the entry.
    assert isinstance(await runs.reconcile("a", timeout=0.05), Pending)

    # A finishes before the caller's next poll.
    gate.set()
    task_a = runs.task_for("a")
    assert task_a is not None
    await task_a
    assert task_a.done()

    # An unrelated run is launched (this is what used to reap A).
    assert isinstance(
        _start(runs, _StubCaller(results=[_ok("B-result")]), invoke_id="b"),
        Started,
    )
    # A's finished-but-pinned result must NOT have been reaped away.
    assert "a" in runs

    # The caller's next poll delivers A's original result, not NotFound.
    outcome = await runs.reconcile("a", timeout=5)
    assert isinstance(outcome, Done), f"expected Done, got {type(outcome).__name__}"
    assert outcome.result.results[0].value == "A-result"
    assert "a" not in runs
    # Drain B.
    await runs.reconcile("b", timeout=5)


@pytest.mark.asyncio
async def test_cap_counts_inflight_not_finished_undelivered():
    """The cap bounds live (subprocess-holding) runs, not finished ones still
    parked for delivery. A run that finished after being polled-to-Pending must
    not consume a cap slot — otherwise the cap fix that keeps it parked (H1)
    would deadlock new launches."""
    runs = BackgroundRuns(max_outstanding=lambda: 1)
    gate = threading.Event()
    _start(runs, _StubCaller(results=[_ok()], gate=gate), invoke_id="a")
    # Poll -> Pending pins A; then let A finish.
    assert isinstance(await runs.reconcile("a", timeout=0.05), Pending)
    gate.set()
    task_a = runs.task_for("a")
    assert task_a is not None
    await task_a
    # A is finished-but-pinned (still in registry). Under cap=1 a naive
    # all-entries count would reject B; the in-flight count is 0, so B starts.
    assert "a" in runs
    assert isinstance(
        _start(runs, _StubCaller(results=[_ok()]), invoke_id="b"),
        Started,
    )
    # Drain.
    await runs.reconcile("a", timeout=5)
    await runs.reconcile("b", timeout=5)


@pytest.mark.asyncio
async def test_default_cap_reads_env(monkeypatch):
    """With no injected provider, the cap is read from CALLSTACK_MAX_BACKGROUND
    *per start*, so a host changing it mid-process is honored."""
    monkeypatch.setenv("CALLSTACK_MAX_BACKGROUND", "1")
    runs = BackgroundRuns()
    gate = threading.Event()
    assert isinstance(
        _start(runs, _StubCaller(results=[_ok()], gate=gate), invoke_id="a"),
        Started,
    )
    assert isinstance(
        _start(runs, _StubCaller(results=[_ok()], gate=gate), invoke_id="b"),
        CapReached,
    )
    gate.set()
    await runs.reconcile("a", timeout=5)


@pytest.mark.asyncio
async def test_clear_forgets_entries_without_cancelling():
    runs = BackgroundRuns()
    gate = threading.Event()
    _start(runs, _StubCaller(results=[_ok()], gate=gate))
    task = runs.task_for("iid")
    assert task is not None
    runs.clear()
    assert "iid" not in runs and len(runs) == 0
    # clear() must not cancel the underlying task.
    assert not task.cancelled()
    # Drain the orphaned task so pytest doesn't warn.
    gate.set()
    await task
