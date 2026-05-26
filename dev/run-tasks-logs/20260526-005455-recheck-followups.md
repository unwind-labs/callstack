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

## Task 6 — Dead-code sweep ✅
- **Commit:** `8902368`
- **Files:** `plugins/callstack/agent_callstack/driver.py`, `frames.py`, `__init__.py`
- **Changes:** Removed `Driver._find_parent` (+ updated stale docstring ref); replaced `__import__("threading")` with `threading.Lock()`; relocated `CALL_TREE_SCHEMA_VERSION` below the import block; dropped `_STATUS_FROM_STATE` re-export from frames; dropped unused `read_finalize_wait_seconds` re-export from `__init__`.
- **Verification:** repo-wide grep confirmed zero callers for each deletion. 356 pass / 8 fail (baseline unchanged). ruff F401 count 40→38.
- **Note:** child also folded this log file's pending working-tree update into the same commit — harmless.

## Task 7 — Test M-batch (T-M1/M3/M4) ✅
- **Commit:** `c323c53`
- **Files:** `tests/test_trace.py`, `tests/test_lifecycle_hardening.py`
- **Changes:**
  - T-M1: `start.set()` moved after all 8 threads are spawned so the SEC-007 race actually races; WHY comment added.
  - T-M3: planted root-frame `schema_version` 1→"2" matching canonical on-disk shape `Tree.from_dict` requires.
  - T-M4: lone `get_event_loop().run_until_complete()` test converted to `@pytest.mark.asyncio` + `async def`.
- **Tests:** 356 pass / 8 fail (baseline unchanged).

## Task 8 — DOC-M4 README parallel-nested vs CONC-1/2 ✅
- **HEAD commit:** `8297fa2` (substantive content in `3ff1328`)
- **Files:** `dev/TODO.md`, `README.md`
- **Decision:** reclassify-not-live (not "fix and check off via a fix commit") — code analysis shows neither deadlock is reachable in the current architecture.
- **Justification:**
  - Per-turn semaphore acquire/release (`channel.py:469,484`) — slot is released before the child turn runs, so nested CALLs never stack semaphore holds.
  - Inline single-child nested drive happens AFTER the parent turn releases (`driver.py:677`).
  - MCP server is per-session stdio (`plugin.json`), so `_IN_FLIGHT_SEMAPHORE` / `_RUN_POOL` module globals never span nesting levels.
- **Outcome:** CONC-1 and CONC-2 marked `[x]` with code-ref justification; new open item **CONC-4** captures the real residual concern (aggregate subprocess memory across wide+deep trees, NOT a deadlock); README showcase gained an honest per-level-concurrency note pointing at the Configuration knobs.

## Task 9 — Remaining mediums batch ✅
- **HEAD commit:** `3eacdfd` (5 commits total)
- **Tests:** 362 pass / 8 fail (baseline unchanged); +6 new tests.
- **Per-item:**
  - **C-M2 + R-M4** (`85897fb`, driver.py + state.py): extracted `child_id` once behind an `AwaitingChild` invariant assert; R-M4 proved the cid==ec mismatch unreachable (both producers read from the same AwaitingChild state; `_propagate_lock` serializes) and documented the invariant — kept fail-loud `AssertionError` rather than adding dead no-op.
  - **R-M3** (`848ca83`, trace.py + test_trace.py): `threading.Lock` around `TraceWriter` mkdir+open+write so concurrent appends >PIPE_BUF can't interleave; regression test = 8 threads × 25 large entries, every line parses.
  - **R-M2** (`722041f`, analysis.py + test_analysis.py): streaming `_iter_jsonl` helper; `trace_events`/`session_messages` no longer hold whole-file string; `session_stats` rewritten to stream in O(1) memory (no SessionMessage list).
  - **M-A3** (`ab3e6d5`, mcp_server.py + test_lifecycle_hardening.py): `_invocation_identity` → `_resolve_invocation_identity` (house `_resolve_*` verb signals the env mutation). Chose rename over split — only 2 call sites; splitting risks DRY-102 drift.
  - **R-M1 + T-M2** (`3eacdfd`, channel.py + test_channel.py + test_channel_pool.py): `_fire_on_session_id` gained optional log sink, ClaudeChannel reuses it instead of drifted inline copy; `ClaudePool` takes injectable monotonic clock, LRU tests now use deterministic `_FakeClock`. Bundled in one commit because both touch channel.py.

## Task 10 — All LOW findings ✅
- **Commits (3):**
  - `bc111b9` — 9 code LOW findings (frames `nested_by_key` rename, reporter `flock→lockf` doc fix, channel `shutdown_pool` comment, state.py uuid hoist, analysis `_parse_ts` `Z` fix, report.py `from_context` cleaned, `__init__.py` 5 dead re-exports dropped F401 19→14, background.py `reap()` crash-finalize symmetry, env.py reader-style uniformity)
  - `79b075a` — analysis CLI L-A1: trace_file made required positional in all three scripts (stale 'call_traces/' default removed); new `tests/test_analysis_cli.py` smoke-tests all 4 CLI wrappers via `--help` + no-arg exit-2
  - `f4abd30` — test LOW fixes + DOC-L1: `boom` stub gains `preallocated_session_id=None`; `_propagate_up_serializes_concurrent_callers` uses `monkeypatch` fixture; dead `_no_real_projects` setup removed; new test pinning clock-skew clamp; README `call_traces/` → `call_trace.jsonl` in both spots
- **Deliberately skipped (with justification):**
  - **L-A5** (shared `_cli.py` for sys.path bootstrap + `_resolve_prefix`): child judged extraction more churn than value — `_resolve_prefix` signature differs across callers; the bootstrap must run before any shared module import anyway
  - **DOC-L2** ("only seam" stale): already addressed in Task 3 — README now reads "the live subprocess seam" alongside "ScriptedChannel: in-memory channel seam for tests"
  - **DOC-L3** (PRDs unmarked historical): `docs/` is gitignored — PRDs aren't versioned, so the reviewer's concern about a reader mistaking an old PRD for current spec doesn't apply to the published repo
  - **DOC-L4** (unwind PyPI claim unverifiable): genuinely unverifiable offline; maintainer knows the state. Per Rule 12 (fail loud), didn't silently soften the wording
- **First Task-10 child** hit the 600s timeout but its two committed batches (`bc111b9`, `79b075a`) landed cleanly; remaining LOWs finished in the parent session as commit `f4abd30`.

---

## Final summary

**Branch:** `main` — 17 commits ahead of starting HEAD `330568b`.
**Final HEAD:** `f4abd30`.
**Test suite:** 379 passing, 0 failing (the 8 "environmental" failures noted in Tasks 1–9 were artifacts of running pytest inside a forked `/call` subprocess; running the full suite directly on final HEAD is fully green).

**Coverage of the consolidated /recheck report:**
- **HIGH (3/3):** H1, H2, H3 → all fixed.
- **MEDIUM (14/14):** C-M1, C-M2, R-M1, R-M2, R-M3, R-M4, M-A1, M-A2, M-A3, T-M1, T-M2, T-M3, T-M4, DOC-M1, DOC-M2, DOC-M3, DOC-M4 → all addressed (DOC-M4 reclassified-not-live with code refs; CONC-1/CONC-2 closed, CONC-4 created).
- **LOW (~16/~20):** code + test + DOC-L1 fixed; 4 LOWs explicitly skipped with justification (L-A5, DOC-L2 redundant, DOC-L3 moot due to gitignore, DOC-L4 unverifiable).

**Notable judgment calls:**
- R-M4: child proved the `cid==ec` mismatch unreachable rather than adding a dead no-op branch — kept fail-loud `AssertionError` and documented the invariant.
- M-A3: rename over split — 2 call sites only; splitting risked DRY-102 drift.
- DOC-M4: refused to add a false deadlock caveat to README; instead reclassified CONC-1/CONC-2 with code-ref proof and split off the real residual concern (subprocess memory) as new CONC-4.
- T-M3: kept fail-loud — production schema_version is `"2"` (string), fixture corrected to match.

**Total LOC delta:** ~17 commits, mostly small surgical changes. No behavior regressions; +24 new tests across the run.
