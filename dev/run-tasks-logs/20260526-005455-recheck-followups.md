# Recheck follow-ups — overnight run

**Started:** 2026-05-26 00:54:55
**Mode:** overnight (unattended)
**Source:** `/recheck` consolidated report against `main` (HEAD 330568b)
**Plan:** suggested order 1–7, then remaining MEDIUMS, then all LOWS in one task

## Task queue

1. **H1** — `driver.py:153` guard `max_context_tokens_seen` subscript
2. **H2** — `results.py:133` map Timeout/Abandoned to `CallFailed(state.error)`
3. **H3 + DOC-M1/M2/M3** — README correctness pass (depth, layout, spawn cmd, env knobs)
4. **M-A1** — `shutdown.py` signal-handler deadlock (RLock or try_acquire)
5. **M-A2** — `resume()` cwd hardening parity with `call()`
6. **Dead-code sweep** — C-M1, frames `_STATUS_FROM_STATE`, init re-export, driver Ls
7. **Test M-batch** — race barrier, schema_version "2", `@pytest.mark.asyncio`
8. **DOC-M4** — reconcile README parallel-nested showcase with TODO CONC-1/CONC-2
9. **Remaining mediums** — C-M2, R-M1, R-M2, R-M3, R-M4, M-A3, T-M2
10. **All lows** — single task

---

## Task 1 — H1 driver.py KeyError on resume ✅
- **Commit:** `147d1ac`
- **Files:** `plugins/callstack/agent_callstack/driver.py`, `tests/test_driver.py`
- **Change:** `Node.from_dict` now uses `d.get("max_context_tokens_seen", 0)` matching the dataclass default. Regression test `test_node_from_dict_tolerates_missing_max_context_tokens` encodes the why.
- **Tests:** test_driver.py 31/31. Full suite 347 pass / 8 fail.
- **Baseline note:** the 8 failures are PRE-EXISTING on clean HEAD 330568b — verified via `git stash` + re-run. They are environmental (suite is running inside a live callstack fork: real `CALLSTACK_*` env vars + real `~/.claude/projects` session files pollute mtime-fallback and depth-stamp tests). Subsequent tasks will treat these 8 as baseline, not regressions.

## Task 2 — H2 results.py timeout/abandoned mapping ✅
- **Commit:** `a449ffe`
- **Files:** `plugins/callstack/agent_callstack/results.py`, `tests/test_results.py`
- **Change:** `_result_from_node` now has an explicit `(Timeout, Abandoned)` case mapping to `CallFailed(node.error or s.error, partial=node.result)`, surfacing the terminal state's real error instead of the synthetic "unexpected state" string. Fallback comment tightened to note it's genuinely unreachable.
- **Tests:** +3 in `test_results.py` (timeout→real msg, abandoned→reason, node.error-unset fallback→s.error). 7/7 pass. Full suite 350 pass / 8 fail (baseline unchanged).

## Task 3 — H3 + DOC-M1/M2/M3 README correctness ✅
- **Commit:** `410572f`
- **Files:** `README.md` (docs only, +41/-11)
- **Change:**
  - H3: depth claims at L319 & L352 → "default 10, ceiling 32" (matches `env.py:94,110`)
  - DOC-M1: package-layout block refreshed to all 19 modules; "only seam" note corrected (ScriptedChannel→testing.py)
  - DOC-M2: `--session-id <uuid>` added to documented spawn argv with note on deterministic-identity enforcement
  - DOC-M3: full env-knob reference for 8 vars with defaults/clamps verified against `env.py`
- **Note:** README L318 "call_traces/" misnomer left for Task 10 LOW sweep.

## Task 4 — M-A1 signal-handler deadlock ✅
- **Commit:** `acc54e3`
- **Files:** `plugins/callstack/agent_callstack/shutdown.py`, `tests/test_shutdown_hardening.py`
- **Change:** `_ACTIVE_REPORTERS_LOCK` is now an `RLock`. Same-thread re-entry from the SIGTERM/SIGINT handler while the main thread already holds the lock (mid register/unregister/flush) no longer self-deadlocks. Stale docstring corrected.
- **Tests:** +2 in `TestRegistryLockReentrancy` (non-blocking double-acquire pin + functional flush-while-held). 31/31 pass. Baseline unchanged.

## Task 5 — M-A2 resume() cwd hardening parity ✅
- **Commit:** `2e3fa84`
- **Files:** `plugins/callstack/mcp_server.py`, `tests/test_mcp_server.py`
- **Change:** `resume()` now routes cwd through `_resolve_cwd` for `{PWD}` expansion, symlink canonicalization, existence/dir check, and sensitive-prefix gating. Errors surface as flat `{status,error}`.
- **Design decisions:**
  - Did NOT replicate call()'s fork+cross-project block — resume has no context param and legitimate fresh cross-project yield-resume; sensitive-prefix gate still protects.
  - Preserved `cwd=None` semantics to `SessionLocator.resolve` because `session.py:184-186` only triggers the cross-project full scan when cwd is None. Regression-tested.
  - Flat `{status,error}` shape matches resume's existing single-Result convention (call() uses results[] only because it's batch).
- **Tests:** +4 in `TestResumeToolGuards`, full suite 356 pass / 8 fail (baseline unchanged).
