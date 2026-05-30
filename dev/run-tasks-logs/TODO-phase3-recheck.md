# /recheck findings — Phase 3 (MergeEngine + liveness seam)

Reviewed the uncommitted Phase 3 diff (liveness.py new; frames.py, report.py, reporter.py; tests). Implementation is functionally correct, full suite green (620+9), coverage 95.81%, ruff clean. All target invariants verified preserved: cross-process fcntl merge lock, content-hash write-skip (excludes ended_at), no-root guard, nested report.partial.yaml fallback, PERF-104 dir-mtime fast path, parsed-frame LRU, orphan-reconciliation-on-cache-hit, mutation-safety deep-copy, and the write_report lock-race fix (both report.write_report and reporter._do_merge now route through MergeEngine under the lock). reconcile_orphans is pure over injected Liveness + OrphanPolicy, reads the clock once.

## CRITICAL
(none)

## HIGH
- **H1 — Dead duplicate dead-check helper.** `frames._frame_writer_is_dead` (old, uses module `_pid_alive` + ttl) is now used ONLY by its own test class `TestFrameWriterIsDead`; production reconciliation goes through the new `_writer_is_dead` (injected `Liveness`) via `reconcile_orphans`. Two near-identical functions; the old one is dead production code kept alive by its tests — the "replace, don't layer" anti-pattern the RFC's testing strategy calls out. **FIX (this commit):** delete `_frame_writer_is_dead` and `TestFrameWriterIsDead`; `_writer_is_dead` is covered by the FakeLiveness `reconcile_orphans` tests (dead / alive / ttl-opt-out / unknown-age / pid-reuse-past-ttl).

## MEDIUM (left for review — not fixed autonomously)
- **M1 — Back-compat liveness adapter layer.** `_reconcile_orphan_states` → `_ModuleLiveness` → `_pid_alive` → `SYSTEM_LIVENESS` → `OsLiveness.pid_alive` is a 4-hop chain whose only purpose is to let the legacy `TestReconcileOrphanStates` tests keep monkeypatching `fr._pid_alive`. Cleaner end-state (per RFC "replace, don't layer"): `_reconcile_orphan_states` uses `SYSTEM_LIVENESS` directly; delete `_pid_alive` + `_ModuleLiveness`; migrate `TestReconcileOrphanStates` to monkeypatch `os.kill` (or inject via reconcile_orphans). Deferred because it requires rewriting 4-5 integration tests and risks subtle coverage gaps in an unattended run — low correctness risk, pure tidiness.
- **M2 — Redundant pid-liveness tests.** `test_frames.TestPidAlive` now duplicates `test_liveness.py`'s OsLiveness pid-liveness tests. Once M1 lands, delete `TestPidAlive` (test_liveness is the canonical home).
- **M3 — Unused store-injection seam.** `MergeEngine(ctx, *, store=...)` and `_MergeFileStore` exist but no fake is ever injected (the RFC deliberately keeps fcntl/os.replace as real-file ops under tmp_path). The seam is harmless and clean, but if no injection materializes it's mild YAGNI — consider inlining `_MergeFileStore` into `MergeEngine` or keep as the documented FS boundary.

## Decision (overnight mode)
Fixing H1 now. M1–M3 are maintainability-only (no correctness/security/perf impact) and left for morning review per the overnight policy (fix CRITICAL+HIGH automatically; surface MEDIUM).
