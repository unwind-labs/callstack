# /recheck findings — Phase 4 (identity resolver)

Reviewed the uncommitted Phase 4 diff (invocation.py, __init__.py, mcp_server.py, reporter.py, channel.py + migrated tests). Implementation is correct and faithful to RFC #4. Full suite green (624), coverage 95.73%, ruff clean.

Verified:
- The root-vs-nested decision is now ONE pure `resolve_identity(inputs, *, dir_exists, most_recent_session) -> ResolvedIdentity`. The `os.environ.pop` (DRY-102) is deleted. Boundary↔run determinism holds WITHOUT env mutation: the boundary mints the fresh id and threads it into the Caller as `explicit_invoke_id`, and the dir_exists validation lives inside the single decision — so both sides reach the same result. Nested still keyed on "inherited CALLSTACK_ROOT_* AND its invocation dir exists" (the dir was already the stale-env criterion in the old boundary code), so production behavior is equivalent.
- Lazy-env contract preserved: `_capture_inputs` snapshots env/cwd/pid at decision time (in `resolve()`/`identity_for_boundary`), not at Caller construction.
- Warning-as-data: stale-env rejection AND explicit-id-ignored-when-nested are `ResolvedIdentity.warning`, printed at the MCP edge. The explicit-ignored case is now SURFACED (was a silent drop).
- `_InvocationContext` is the sole `{log_dir}/{invoke_id}/...` layout owner: `mcp_server._report_path`/`_log_dir` deleted; reporter._finalize_own_frames and channel._process_log_path construct a context and read `.frames_dir`/`.lock_path`/`.invocation_dir`.
- L1 invariant preserved (partial env -> `root_identity()` None -> root by all callers). CORR-101 max_depth child_env stamping untouched. `_resolve_cwd` security gating untouched. Sync-budget in-flight code integrated, not reverted.

## CRITICAL
(none)

## HIGH
- **H1 — Dead duplicate `effective_log_dir`.** `InvocationFactory.effective_log_dir()` is now dead production code (only `TestEffectiveLogDir` calls it); `resolve_identity` inlines the same `{cwd}/.claude/callstack/log` layout (invocation.py:133, covered). Duplicated layout logic. **FIX (this commit):** delete the method + `TestEffectiveLogDir`. (`effective_cwd` is NOT dead — still used by `Caller._driver_for` at __init__.py:312.)

## MEDIUM (left for review)
- **M1 — `parent_project_cwd` predicate divergence.** It still uses presence-only (`env.root_identity() is not None`) while `context()`/`resolve()` now also validate `dir_exists`. Benign in practice (differs only in the rare stale-env case, and for the MCP boundary `explicit_cwd == getcwd` there, so both resolve to the same project). Left unchanged to preserve the L1 invariant test and minimize risk; consider unifying behind a shared "am I nested?" check that takes the dir probe.
- **M2 — invocation.py 93% (defensive branches).** Lines 74-75/145-146/218-219 are `except OSError` guards in `_default_dir_exists`/`_safe_getcwd`/`parent_project_cwd` — hard to exercise without forcing OSError. Total coverage 95.73% >= gate. Optional: add monkeypatch-OSError tests.

## Decision (overnight mode)
Fixing H1 now. M1-M2 are maintainability/coverage-polish only (no correctness/security/perf impact) — left for morning review.
