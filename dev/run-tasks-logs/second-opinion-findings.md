# Second-opinion adversarial review — refactor/deepen-modules-overnight

Independent re-review of facb304 / 298518d / e1f6429 / 3d2141f, cross-checking
`dev/run-tasks-logs/recheck-findings.md`. Verdict matches the first pass: the
four facades are genuinely deep, migrations are behavior-preserving, no CRITICAL.
I **confirm H1 as a real bug**, **refine its proposed fix** (the first review's
fix is half-right and, as written, trades the bug for an unbounded leak), and add
a few observations the first pass missed. Points (2) and (4) came back clean.

---

## CONFIRMED (agree with first review)

- **H1 — `BackgroundRuns.reap()` loses a Pending'd run's result. REAL.** I traced
  the exact interleaving and it holds, but note the loss spans *two* `reconcile`
  calls, not one: `reconcile("X")` captures `run` locally before awaiting, so a
  single poll is safe even if reaped mid-await. The bug is: poll#1 → `Pending`
  (entry kept, coroutine ends) → X finishes → `start("Y")` → `reap()` pops X
  (`X.task.done()`) → poll#2 → **`NotFound`**. The `MultiResult` is stranded in
  the orphaned task object. Confirmed HIGH.
- **H2** — coverage gap masks H1. Confirmed.
- **L1** — `parent_project_cwd()` uses `env.in_nested_invocation()` (ROOT_INVOKE_ID
  alone) while `context()` uses `env.root_identity()` (needs BOTH env vars). Read
  both (`env.py:231` vs `:254`): genuinely divergent when only ROOT_INVOKE_ID is
  set. Pre-existing on `main`; agree it should be unified on `root_identity() is
  not None`.
- **L2 / L3** — dummy `cwd=""` in `_finalize_crashed`; `CancelledError`
  propagates out of `reconcile` without popping. Both confirmed, both minor.

---

## HIGH — refines the first review

### H1-fix — cap-on-in-flight is right; "drop reap / evict only delivered" is underspecified and leaks

The first review's fix is two parts. Part one (**gate the cap on in-flight, not
total**) is correct and sufficient on its own to remove the *cap pressure* that
motivates reaping — finished runs hold only a `MultiResult` (cheap), not a live
`claude` subprocess (the 0.5–2 GB the cap actually exists to bound). Keep it:

```python
def _inflight(self) -> int:
    return sum(1 for r in self._runs.values() if not r.task.done())
```

Part two — "drop `reap()` from `start()`; let `reconcile` be the sole pop site;
if you still want memory bounding, evict only *delivered* entries" — does **not**
close the leak, because *delivered entries are already popped by `reconcile`*.
The only entries ever present are in-flight or finished-undelivered, so "evict
only delivered" evicts nothing, and a host that fires background runs and never
polls grows `_runs` without bound. Conversely, any reaper that drops
finished-undelivered entries *can* drop a Pending'd result — which is H1 again.

The real constraint the first review skipped: **you cannot distinguish
"fire-and-forget, abandoned" from "Pending'd, will be polled again" by task state
alone** — both are `done() and undelivered`. The missing signal is caller intent.
Record it:

```python
@dataclass
class _Run:
    task: ...; report_path: ...; log_dir: ...; invoke_id: ...
    polled: bool = False          # set True once reconcile returned Pending

# in reconcile, the TimeoutError branch:
    run.polled = True
    return Pending(invoke_id, run.report_path)

# reap drops only genuinely-abandoned runs (finished, never polled):
def reap(self) -> None:
    stale = [i for i, r in self._runs.items() if r.task.done() and not r.polled]
    ...
```

Net: cap counts in-flight; `reconcile` stays the sole *result* deliverer; `reap`
still bounds memory for true fire-and-forget but can never strand a result the
caller signaled interest in. This is the minimal fix that satisfies both H1 and
the leak. `task_for()` stays meaningful and unchanged (tests still await a run
deterministically before reconciling) — answering the first review's open
question: **yes, keep it.**

---

## LOW — missed by the first review

### L5 — `os.environ` mutation races concurrent background workers (pre-existing, very low prob)
`mcp_server._invocation_identity` pops `ENV_ROOT_*` (`mcp_server.py:132-133`) on
the event-loop thread, while `InvocationFactory.context()` reads the same vars
**lazily on a `to_thread` worker** for a *different* in-flight background call.
Per-key `os.environ` ops are atomic under the GIL, and the pop only ever *removes*
root vars (and only after the root dir failed validation), so the worst realistic
outcome is "mints fresh instead of nested" — the safe direction. Requires the
confluence of: nested-MCP context + stale/invalid root dir + overlapping
background calls. Pre-existing (the `to_thread` + lazy-env model predates this
branch; Task 3 only made the lazy read explicit). Flagging for awareness, not
action. If ever tightened: snapshot the resolved identity on the loop thread
(already done — `_invocation_identity` returns concrete values) and pass it into
the Caller so the worker never re-reads env. The Caller already accepts an
explicit `invoke_id`/`log_dir`, so the worker's env re-read is partly redundant
with values already resolved on the loop thread — a latent inconsistency more
than a bug.

### L6 — `reap()`'s `.exception()` retrieval is correct but subtle
`background.py:190` calls `run.task.exception()` to consume a pending exception so
asyncio doesn't warn at GC. After the H1-fix this only runs for abandoned
fire-and-forget runs, which is exactly right — but add a one-line comment that
discarding the exception here is intentional (the run was never awaited, no one
is listening), so a future reader doesn't "fix" it into a swallowed error.

---

## CLEARED — pressure-tested, no finding

### (2) `InvocationReport.seal()` — no exception-safety regression
`report.py:148-160` is a faithful lift. Old `Caller._invoke`/`resume` finally
blocks ran `wait_for_terminal_signals(...)` **then** `reporter.finalize(tree)` as
two sequential statements — if the wait raised, finalize was skipped. `seal()`
preserves that exact order and that exact skip behavior; the budget is read the
same way (`read_finalize_wait_seconds()` when arg is None). `resume()` calls
`seal` unconditionally, matching old `resume` (tree is always the loaded
snapshot). No path finalizes-then-waits, none double-finalizes. Clean.
(First review's M2 — *seal is only indirectly tested* — stands as a nice-to-have,
not a correctness gap.)

### (3) `InvocationFactory` lazy env reads — no ThreadPoolExecutor race
`Driver.run` fans siblings out on a `ThreadPoolExecutor`, but those workers run
**turns** (`channel.run_turn`), not factory resolution. `parent_project_cwd()`,
`context()`, and `child_env()` are all invoked on the *calling* thread in
`_invoke`/`_driver_for` **before** `driver.run` (`__init__.py:236-253`). No pool
worker ever touches the factory, so there is no env-vs-pool race within a single
invocation. (The only cross-thread env exposure is the separate background-runs
path — see L5.)

### (4) flock + debounce — no deadlock
`_interprocess_lock` (`reporter.py:366`) uses a single `fcntl.lockf(LOCK_EX|
LOCK_NB)` retry loop with a 30 s deadline after which it **proceeds without the
lock** — a deliberate anti-deadlock escape, not a hang. Lock acquisition order is
always `_thread_lock` → flock (`_do_merge` requires the thread lock, takes the
flock inside); a single interprocess lock means no cross-process lock-ordering
cycle is possible. `_emergency_finalize_on_shutdown` uses `acquire(timeout=0.5)`
and bails, so shutdown can't wedge. OS releases the flock fd on process death, so
a crashed sibling can't permanently block the root. The facade (Task 1) did not
touch any of this. Clean.

---

## Ousterhout / scope check (agree)
`InvocationReport`, `BackgroundRuns`, `InvocationFactory` are all deep (small
typed interface, large hidden implementation). No speculative abstraction. The
only ergonomic smells are L2 (dummy cwd) and L5's redundant worker-side env read.

## Recommended remediation order (overnight)
1. **H1 + H2** with the `polled`-flag refinement above (the only correctness item).
2. **M1** remove dead `wait_for_terminal_signals` import in `__init__.py:65`.
3. **L1** unify the nested predicate on `root_identity() is not None`.
4. L2 / L6 comment-and-ergonomics; L3 / L5 leave documented. M2 optional test.
