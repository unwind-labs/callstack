"""Unit tests for the shared liveness + wall-clock seam.

The seam exists so orphan reconciliation (and, later, the identity resolver) can
inject a `Liveness` instead of monkeypatching `os.kill` / `time.time` scattered
across modules. These pin the production adapter's PID-liveness mapping and the
`OrphanPolicy` data carrier, and demonstrate the `FakeLiveness` shape the rest
of the suite uses.
"""

from __future__ import annotations

import os

from agent_callstack.liveness import SYSTEM_LIVENESS, OrphanPolicy, OsLiveness


class FakeLiveness:
    """Reference fake: a controllable live-pid set + a frozen clock. This is
    the shape reconcile tests inject instead of patching module internals."""

    def __init__(self, *, alive: set[int], clock: float):
        self._alive = alive
        self._clock = clock

    def pid_alive(self, pid: int) -> bool:
        return pid in self._alive

    def now(self) -> float:
        return self._clock


def test_system_liveness_is_an_osliveness():
    assert isinstance(SYSTEM_LIVENESS, OsLiveness)


def test_own_pid_is_alive():
    assert OsLiveness().pid_alive(os.getpid()) is True


def test_invalid_pid_is_dead():
    lv = OsLiveness()
    assert lv.pid_alive(0) is False
    assert lv.pid_alive(-5) is False
    assert lv.pid_alive("nope") is False  # type: ignore[arg-type]


def test_permission_error_means_alive(monkeypatch):
    # EPERM: the process exists, we just can't signal it — the safe direction.
    monkeypatch.setattr(os, "kill", lambda _pid, _sig: (_ for _ in ()).throw(PermissionError()))
    assert OsLiveness().pid_alive(99999) is True


def test_process_lookup_means_dead(monkeypatch):
    monkeypatch.setattr(os, "kill", lambda _pid, _sig: (_ for _ in ()).throw(ProcessLookupError()))
    assert OsLiveness().pid_alive(99999) is False


def test_generic_oserror_means_dead(monkeypatch):
    monkeypatch.setattr(os, "kill", lambda _pid, _sig: (_ for _ in ()).throw(OSError("boom")))
    assert OsLiveness().pid_alive(99999) is False


def test_now_returns_wall_clock(monkeypatch):
    monkeypatch.setattr("agent_callstack.liveness.time.time", lambda: 1234.5)
    assert OsLiveness().now() == 1234.5


def test_orphan_policy_carries_ttl():
    assert OrphanPolicy(ttl_seconds=60.0).ttl_seconds == 60.0


def test_fake_liveness_shape():
    lv = FakeLiveness(alive={7}, clock=100.0)
    assert lv.pid_alive(7) is True
    assert lv.pid_alive(8) is False
    assert lv.now() == 100.0
