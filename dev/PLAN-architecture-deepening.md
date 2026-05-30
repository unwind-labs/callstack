# Sequencing plan: architecture deepening (RFCs 1–5)

Implements the five RFCs in `dev/RFC-deepen-*.md` and `dev/RFC-tidy-env-and-trace.md`.
Ordered by dependency, then by risk. Every step ends with the full suite green
(`python -m pytest -q`, currently 594 passing) and one small commit.

## Dependency graph

```
Phase 0  shared Liveness/Clock/FS seam ──┐──────────────┐──────────────┐
                                         │              │              │
Phase 1  #1 property-Node + TurnExecutor ┘              │              │
            │ (NodeView.seal becomes bare assignment)   │              │
            ▼                                            │              │
Phase 2  #3 node-sealing  ◀── needs #1 + Phase 0 Liveness/Clock        │
                                                         │              │
Phase 3  #2 merge-pipeline ◀── needs Phase 0 Liveness ───┘             │
                                                                        │
Phase 4  #4 identity resolution ◀── needs Phase 0 FS probe ────────────┘
            ⚠ collides with the in-flight sync-budget edits to mcp_server.py
                                                                        
Phase 5  #5 tidy (env table + trace split) — independent, last
```

## Why this order

- **Phase 0 first.** RFCs 2, 3, and 4 each independently called for a `Liveness`/`Clock`/filesystem seam. Build it once as a standalone module with its own tests so the later phases consume one abstraction, not three. Lowest risk, highest leverage; no behavior change.
- **#1 before #3.** The property-`Node` migration (delete `_denormalize`, derive fields from `state`) is what lets `NodeView.seal` collapse to a bare `node.state = …`. Both RFCs flagged the same external-writer audit; doing #1 first means #3 lands clean.
- **#2 and #3 are parallel-ish** once Phase 0 + #1 are in; sequence #3 then #2 to keep `Node`/`state` churn contiguous.
- **#4 last among substantive work** because it rewrites `mcp_server.py`, which currently carries unrelated in-flight changes. Sequence it after that is resolved to avoid a merge fight.
- **#5 last / optional.** Pure tidiness, no correctness or testability gain.

## Phases

### Phase 0 — shared seam (no behavior change)
- New `clock.py` / `liveness.py` (or one `runtime_ports.py`): `Liveness.pid_alive`/`writer_is_dead`, `Clock.monotonic`/`now`/`sleep`, a `dir_exists` probe. Production adapters wrapping `os.kill`/`time`/`pathlib`; in-memory fakes for tests.
- Pure unit tests for each adapter + fakes.
- Commit: `feat(ports): shared Liveness/Clock/FS seam`.

### Phase 1 — #1 turn-execution seam
1. Audit + convert `Node` denormalized fields to read-through properties over `state`; delete `_denormalize`. Migrate every external writer (`frames.py`, `reporter.py`, `terminal_wait.py`) to set `node.state`. Tests green. Commit.
2. Extract `TurnExecutor` (RunTurn orchestration) returning `TurnOutcome`; driver `_run_turn` collapses. Tests green. Commit.
3. Formalize the channel `TurnObserver` timing contract + parametrized conformance test (fake + gated real). Commit.

### Phase 2 — #3 node sealing
1. `sealing.py`: `Cause` union, `terminal_state_for`, `NodeView`, `seal_tree`. Adapters for Tree + dict shapes. Commit.
2. Route the four triggers (`terminal_wait` expiry, shutdown emergency, background crash, orphan reconciliation) through `seal_tree`; delete the two walkers. Use Phase 0 `Liveness`. Commit.
3. Sign-off behavior change: unify the abandonment message (drop `"abandoned: "` prefix); update affected assertions. Commit.

### Phase 3 — #2 merge pipeline
1. `MergeEngine` + `FileStore`; extract pure `reconcile_orphans`/`build_merged_report`/`content_hash`; inject Phase 0 `Liveness`. Commit.
2. Collapse `report.merged_document`/`write_report` and `reporter._do_merge`/`_write_partial_if_no_root` onto the engine; fix the `write_report`-skips-the-lock race. Commit.

### Phase 4 — #4 identity resolution (after in-flight mcp_server work resolved)
1. Pure `resolve_identity` over a lazily-captured snapshot + injected `dir_exists`; `ResolvedIdentity` with warning-as-data. Commit.
2. Boundary consumes the single decision; delete `_resolve_invocation_identity` + the `os.environ` pop; route the 4 path-layout joins through `_InvocationContext`. Commit.

### Phase 5 — #5 tidy (optional)
1. `env.py` numeric-reader spec table + parametrized test. Commit.
2. Split `TreeStore` into `tree_store.py` with a one-release re-export. Commit.

## Invariants to hold at every step
- Full suite green before each commit; coverage stays ≥ 92% (`fail_under`).
- Preserve all SEC-/PERF-/REVIEW-/CONC- hardening; never delete a guard without a replacement test.
- One logical change per commit; no drive-by edits (Rule 3).
- Surface any behavior change explicitly in the commit body (Rule 12).
