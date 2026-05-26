# /recheck findings — refactor/deepen-modules-overnight (main..HEAD)

Reviewed commits: facb304 (report.py / InvocationReport), 298518d (background.py /
BackgroundRuns), e1f6429 (invocation.py / InvocationFactory), 3d2141f
(session.most_recent_session move).

**Overall:** a clean, genuinely *deep* set of facades. `report.py`,
`background.py`, `invocation.py` each present a small typed interface over real
complexity (flock/debounce/atomic-write; asyncio lifecycle; nested-vs-root env
heuristic). Migrations preserved behavior; 351 tests pass. Test coverage of the
new boundaries is strong (InvocationReport used in 16 test sites; InvocationFactory
has instance_id-collision, partial-env, cross-project, CORR-101 child_env tests;
most_recent_session has 5 boundary tests incl. PROJECTS_DIR-swap recreation).

One real correctness bug (H1) and its masking coverage gap (H2). Everything else
is hygiene/polish.

---

## CRITICAL
None.

---

## HIGH

### H1 — `BackgroundRuns.reap()` can silently discard a finished run's result
`background.py:132-143` (start→reap+cap) and `181-190` (reap).

`reconcile(timeout)` returns `Pending` and **keeps** the entry when the await
budget elapses but the run is still healthy (correct). But `reap()` — called at
the top of every `start()` — drops **any** entry whose `task.done()` is true,
and cannot distinguish:
  (a) fire-and-forget: started, never reconciled (safe to drop), from
  (b) awaited→`Pending`, the task has since finished, caller will poll again.

Sequence that loses a result:
1. `reconcile("X")` times out → `Pending`, entry kept.
2. X's `call_many` finishes (result now sitting in the task).
3. An unrelated `start("Y")` runs → `reap()` sees `X.done()` → pops X, consumes
   its result/exception.
4. `reconcile("X")` → **`NotFound`**. The `MultiResult` is gone; the host reports
   the background call vanished.

This is plausible in exactly the overnight/batch usage background runs exist for
(fire several, poll them between new launches). The cap's premise is also off:
it counts *all* parked runs (incl. finished-undelivered) and relies on reap to
free slots, which is what forces the lossy drop.

**Fix (preferred):** gate the cap on *in-flight* runs only and stop reaping
undelivered results in `start()`. Let `reconcile` remain the **sole** place a
result is delivered-and-popped (`Done`/`Crashed`).
```python
def _inflight(self) -> int:
    return sum(1 for r in self._runs.values() if not r.task.done())

def start(...):
    if self._inflight() >= cap:          # finished-but-unreconciled don't count
        return CapReached(cap=cap, outstanding=self._inflight())
    ...
```
Drop the `self.reap()` call from `start()`. If memory bounding is still wanted,
add an explicit age-based or count-based eviction that only drops entries already
delivered, never a finished-but-undelivered one. Keep `reap()`'s
exception-consumption only for entries you intentionally evict.

(If you deliberately want fire-and-forget results to be collectable as garbage,
that's defensible — but then `Pending` must not be a reachable state for a run
that will be silently reaped, i.e. the cap fix above is still required.)

### H2 — No test covers the H1 sequence (masks the bug)
`tests/test_background.py`. `test_finished_unreconciled_runs_are_reaped_on_next_start`
only covers case (a) (never reconciled). Add a regression test for case (b):
start → `reconcile` (short timeout) → assert `Pending` → `await task_for(id)` →
`start` a second run → `reconcile(first_id)` → **assert `Done`, not `NotFound`**.
This test should fail on today's code and pass after the H1 fix.

---

## MEDIUM

### M1 — Newly-dead import introduced by the migration
`__init__.py:65` `from .terminal_wait import wait_for_terminal_signals`. The
inline wait+finalize that used it was replaced by `InvocationReport.seal`; the
name is no longer used internally and is not re-exported/consumed from the
package root by any test. Remove it. (`read_finalize_wait_seconds` is likewise
now internally unused but lives in the deliberate `env` compat-re-export block
and is harmless API surface — leave or drop intentionally, not accidentally.)

Note: ruff flags 18 F401s in `__init__.py`, but most are **intentional**
back-compat re-exports genuinely consumed via `from agent_callstack import …`
(`_LiveReporter` → test_lifecycle/test_shutdown; `ENV_ROOT_INVOKE_ID`,
`ENV_ROOT_LOG_DIR`, `ENV_CLAUDE_SESSION` → test_report). Only
`wait_for_terminal_signals` is genuinely dead among the migration's deltas.

### M2 — `seal()` facade behavior not directly boundary-tested
`report.py:148-160`. `seal` (the wait-for-terminal-signals + finalize glue lifted
out of Caller) is exercised only indirectly through Caller in test_api /
test_lifecycle. Low risk (it's a 3-line delegation), but a direct facade test —
build a tree with one non-terminal node, `seal`, assert the node became `Timeout`
and `report.yaml` was written — would pin the contract the lift was meant to
preserve. Optional.

---

## LOW

### L1 — Latent split-brain between `parent_project_cwd()` and `context()`
`invocation.py:58` uses `env.in_nested_invocation()` (true when **ENV_ROOT_INVOKE_ID**
alone is set); `invocation.py:82` uses `env.root_identity()` (requires **both**
ENV_ROOT_INVOKE_ID **and** ENV_ROOT_LOG_DIR). With only invoke_id set,
`parent_project_cwd()` treats the call as nested (returns `getcwd()`) while
`context()` treats it as root. Pre-existing on `main` (the two old helpers had
the same mismatch) — not a regression — but now more conspicuous and even pinned
on one side by `test_partial_root_env_is_treated_as_root_not_nested`. Unify both
on a single predicate (`root_identity() is not None`).

### L2 — `_finalize_crashed` fabricates a dummy `cwd=""` to reach finalize
`background.py:212-220` constructs `InvocationReport(invoke_id, log_dir, cwd="")`
purely to call `finalize_own_frames`, which ignores `cwd` (`report.py:205-213`
uses only `log_dir`+`invoke_id`). The dummy value is harmless but signals a
missing entry point. Consider a `@staticmethod InvocationReport.finalize_frames(
log_dir, invoke_id, *, reason)` so crash cleanup needn't build a half-valid
report. Minor facade ergonomics.

### L3 — `reconcile` propagates `CancelledError` without popping the entry
`background.py:159-166` catches `Exception` (correct — `CancelledError` is
`BaseException` since 3.8). On loop shutdown a cancel propagates out and leaves
the entry parked. Acceptable at shutdown, but worth a one-line doc note that a
cancelled await neither delivers nor evicts.

### L4 — `__init__.py` re-export hygiene
18 F401s mix genuinely-needed back-compat re-exports with the one dead import
(M1). Group intentional re-exports under an explicit `# re-export for back-compat`
+ `# noqa: F401` (or add to `__all__`) so a future dead import can't hide in the
crowd. Pre-existing pattern; the refactor enlarged the crowd.

---

## Ousterhout / over-engineering check
- `InvocationReport`, `BackgroundRuns`, `InvocationFactory` are all deep (small
  interface, large hidden implementation) — the stated goal, achieved.
- `InvocationReport.context` escape hatch is a documented transitional seam for
  Driver/trace wiring; acceptable, not a leak.
- No speculative abstraction spotted. The only ergonomic smell is L2's dummy cwd.

## Suggested remediation order
1. H1 (cap on in-flight + drop reap-in-start) + H2 (regression test).
2. M1 (remove dead import).
3. L1 (unify nested predicate) — small, removes a latent footgun.
4. L2/L3/L4/M2 — polish, optional.
