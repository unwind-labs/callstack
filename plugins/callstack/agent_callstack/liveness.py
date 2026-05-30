"""Shared liveness + wall-clock seam.

A single injectable boundary for two impure questions — "is this process still
alive?" and "what time is it?" — used by orphan reconciliation (`frames`) today
and intended for reuse by the identity resolver (Phase 4) tomorrow. Keeping it
in one module means one production adapter and one set of test fakes, instead of
`os.kill` / `time.time` scattered across modules (which previously forced tests
to monkeypatch `frames._pid_alive`).

The orphan TTL is carried as data (`OrphanPolicy`) rather than read from the
environment inside the reconcile logic, so that logic is a pure function over an
injected `Liveness` + policy — unit-testable with a `FakeLiveness`, no
monkeypatching and no real PIDs.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Protocol


class Liveness(Protocol):
    """Liveness probe + wall clock. The production adapter is `OsLiveness`;
    tests inject a fake with a controlled live-pid set and clock."""

    def pid_alive(self, pid: int) -> bool: ...

    def now(self) -> float: ...


class OsLiveness:
    """Production adapter: `os.kill(pid, 0)` liveness + `time.time()` clock."""

    def pid_alive(self, pid: int) -> bool:
        """True if signal 0 reaches `pid`. False on ESRCH (process gone) or an
        invalid pid; True on EPERM (process exists, we just lack permission).

        Only a liveness probe — it does not verify process identity. A reused
        PID reads as "alive," the safe direction (we skip reconciliation rather
        than falsely abandon a live invocation)."""
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    def now(self) -> float:
        return time.time()


# Process-wide default. Construction is free (no state), so callers can also
# build their own; this exists so call sites don't each re-instantiate.
SYSTEM_LIVENESS = OsLiveness()


@dataclass(frozen=True)
class OrphanPolicy:
    """Wall-clock TTL past which a frame's `writer_pid` is treated as dead
    regardless of the liveness probe — the defense against PID reuse (macOS
    recycles PIDs within a few thousand spawns). `ttl_seconds <= 0` opts out of
    the TTL fallback (liveness probe alone). Resolved from env at the edge and
    passed in as data so the reconcile logic never reads the environment."""

    ttl_seconds: float
