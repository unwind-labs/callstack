# /recheck findings — Phase 5 (env spec table + trace split)

Reviewed the uncommitted Phase 5 diff (env.py, trace.py, new tree_store.py; tests). Behavior-preserving tidy. Full suite green (623), coverage 95.94% (env.py / trace.py / tree_store.py all 100%), ruff clean.

Verified:
- **env table**: the 7 numeric readers collapse to one `_NumericKnob` spec + `_read_numeric` + thin named wrappers. Per-knob policy matches the original EXACTLY — reject `<=0` for max_depth/max_fanout/max_background (counts), reject `<0` for report_debounce/finalize_wait/orphan_ttl/sync_budget (durations, 0 = disable); ceilings max_depth=32, finalize_wait=600, orphan_ttl=86400, sync_budget=86400, none for debounce/fanout/background. Public names + int/float return types unchanged. String readers + ENV_* constants + _DEFAULT_*/_MAX_* constants untouched. `current_depth` (different semantics) correctly left out.
- **trace split**: `TreeStore` + `_json_default` moved to `tree_store.py`; `trace.py` keeps `TraceWriter` and re-exports `TreeStore` for back-compat (`from agent_callstack.trace import TreeStore` still works). No import cycle (suite green). Tests split into `test_tree_store.py` + a re-export-identity assertion.
- Behavior change: NONE (all pre-existing per-knob env tests pass unchanged).

## CRITICAL / HIGH
(none)

## Accepted deviation (no action)
- The fork kept the existing per-knob `test_env.py` tests rather than rewriting them into a parametrized table (as the RFC's testing-strategy suggested). Accepted: the unchanged per-knob tests are the strongest proof the table refactor is behavior-preserving, already give explicit per-knob default+ceiling assertions, and hold env.py at 100% coverage. Parametrizing would add churn/risk for zero coverage gain (CLAUDE.md Rule 2/3). Optional future polish, not a defect.

## Decision (overnight mode)
Nothing to fix. Committing Phase 5 as-is.
