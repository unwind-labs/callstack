# Task 1 — Deepen the report-assembly cluster

## Problem
One concept — *the merged invocation report* — is spread across four files
(`invocation_ctx.py` value, `frames.py` read/merge, `reporter.py` write,
finalize-glue inside `Caller._invoke/_resume`). Tests (`test_report.py`,
`test_orphan_reconciliation.py`, `test_shutdown_hardening.py`, `tests/_helpers.py`)
reach into ~8 underscore names to construct contexts, reporters, write frames by
hand, load frames, and build merged docs. Shallow modules → tests bind to internals.

## Design — one public boundary: `agent_callstack.report.InvocationReport`

A facade that owns the full lifecycle of one invocation's on-disk artifacts.
It **composes** the existing battle-tested internals (no rewrite of the flock /
fsync / PID-liveness / LRU-cache / content-hash-skip machinery — that would be
reckless). The facade is the small interface; `frames/reporter/invocation_ctx`
become clearly-internal implementation.

```python
ROOT_FRAME_KEY = "root"

class InvocationReport:
    def __init__(self, *, invoke_id, log_dir, cwd,
                 frame_key=ROOT_FRAME_KEY, is_nested=False, instance_id=""): ...

    # identity / paths
    invocation_dir / frames_dir / report_path / log_path : Path  (properties)
    invoke_id / cwd / frame_key / is_nested              (read-through)
    context -> _InvocationContext        # escape hatch for Driver/trace wiring
    frame_path(key=None) -> Path
    prefix(kind) -> str

    # live writing
    reporter(*, kind, tasks, started_at) -> _LiveReporter   # Driver.on_progress
    seal(reporter, tree, *, finalize_wait_seconds) -> None  # wait + finalize glue

    # reading / merging
    load_frames() -> dict[str, list[dict]]
    merged_document(*, ended_at) -> Optional[dict]          # None if no root frame
    write_frame(frame: dict, *, key=None) -> Path           # fixtures/tests
    write_report(*, ended_at) -> Optional[Path]             # build + atomic write

    # boundary finalize (MCP)
    finalize_own_frames(*, reason) -> bool
```

`seal()` absorbs the duplicated try/finally inner logic from `Caller._invoke`
and `Caller.resume` (`wait_for_terminal_signals` + `reporter.finalize`).

## Dependency category
**Local-substitutable** (filesystem + `fcntl` flock). Tests already run against
`tmp_path`; the facade keeps that.

## Migration
- New `report.py`. Export `InvocationReport`, `ROOT_FRAME_KEY` from package root.
- Migrate `Caller` (`__init__.py`) to build/seal via `InvocationReport`.
- Migrate `mcp_server.py` finalize call to `report.finalize_own_frames`
  (+ update `test_mcp_server` patch target).
- Migrate `tests/_helpers.py`, `test_report.py`, `test_orphan_reconciliation.py`,
  `test_shutdown_hardening.py` to the facade.
- **Additive, not destructive**: keep existing underscore names in their modules
  so out-of-scope files (`test_lifecycle_hardening`, `test_invariant_child_parent`)
  keep working. The facade wraps; it does not delete.

## Coverage parity — deliberate exception (fail-loud)
Reframe every test whose assertions are about *observable report behavior*
(frame writing, grafting, merged shape, orphan reconciliation, boundary/emergency
finalize) through the facade. **Keep as clearly-labeled white-box** the handful
that assert internal *optimizations* with no honest behavioral expression:
parsed-frame cache, dir-mtime fast-path, content-hash write-skip, deep-copy
independence. Forcing those through a behavioral facade would gut their teeth.
These continue to import `frames`/`reporter` internals directly and say so.

## Success criteria
- `python -m pytest tests/ -q` → 320 passing (no net loss).
- The 3 named test files + `_helpers.py` construct everything via `InvocationReport`
  except the explicitly-labeled optimization tests.
- `Caller` no longer hand-rolls the wait+finalize glue.
