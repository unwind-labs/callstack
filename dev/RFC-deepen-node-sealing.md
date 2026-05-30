# RFC: Deepen node sealing / finalization

_one `seal_tree` walk over a `NodeView` protocol; Timeout-vs-Abandoned as a typed cause picked at the trigger; a shared `Liveness`/`Clock` port to end the monkeypatching_

Labels: enhancement

---

## Problem

A node normally reaches a terminal state through the pure `state.step()`. But **four other paths** seal a node when its own state machine can't, and the shared mechanics are duplicated across two data shapes.

### The four trigger paths

| Trigger | Site | Shape | Terminal | Impure dependency |
|---|---|---|---|---|
| Pre-finalize terminal wait | `terminal_wait.wait_for_terminal_signals` → `expire_to_timeout` | in-memory Tree | `Timeout` | `time.monotonic` poll + JSONL tail |
| Shutdown emergency | `reporter._emergency_finalize_on_shutdown` → `_abandon_tree_nodes_in_place` | in-memory Tree | `Abandoned` | `os.getpid` + signal/atexit |
| Background crash | `background._finalize_crashed` → `report.finalize_own_frames` → `mark_abandoned_in_dict_nodes` | dict frames | `Abandoned` | filesystem + own-pid filter |
| Orphan reconciliation | `frames._reconcile_orphan_states` → `mark_abandoned_in_dict_nodes` | dict frames | `Abandoned` | `os.kill` PID-liveness + wall-clock TTL |

### The duplication that remains (REVIEW-201 only got halfway)

The eligibility *policy* is already shared (`state.is_eligible_for_abandonment`, state.py:137) and `_STATUS_BY_KIND` is single-sourced. What is **not** shared is the walk-mutate-mirror-error-recurse step, which exists as **two walkers over two shapes**:

- `_abandon_tree_nodes_in_place` (reporter.py:500) — in-memory `Tree`/`Node` objects
- `mark_abandoned_in_dict_nodes` (frames.py:194) — dict/YAML frame shape

The same operation is written twice because the data exists in two representations: in-memory `Node` *before* serialization, and frame dicts *after*. A third quasi-walker — `terminal_wait`'s per-node `expire_to_timeout` — applies the same idea one node at a time to `Timeout`. The walkers stay in policy lockstep only by convention ("both consult the same predicate"), and their message wording has already drifted (`"abandoned: {reason}"` in the dict walker vs `"{reason}"` in the Tree walker).

### The testability tax

- Orphan reconciliation can only be tested with real frame files **and** `monkeypatch.setattr(frames, "_pid_alive", …)` / `monkeypatch.setattr(frames, "read_orphan_ttl_seconds", …)` — reaching into module privates.
- The terminal-wait budget logic uses a real `time.monotonic`/`sleep`, so deterministic tests are awkward.

### What is NOT the problem

`shutdown.py` is already a clean, deep signal/atexit registry (REVIEW-202) — leave it alone. The `Timeout` vs `Abandoned` distinction is **principled and load-bearing** (`Timeout` = we waited for an envelope and gave up; `Abandoned` = an external signal sealed us, we never waited) and must survive any unification.

## Proposed Interface

A **C+D hybrid** of four independently-designed interfaces that *converged* on the same spine: one walk over a `NodeView` protocol, with the terminal cause typed and chosen at the trigger. The hybrid takes Design D's pure core + `Liveness`/`Clock` ports (for testability) and Design C's caller ergonomics, and skips Design B's speculative policy/observer/registry machinery.

### Pure core (`sealing.py`) — no `os`, `time`, `signal`, or filesystem imports

```python
@dataclass(frozen=True)
class AbandonCause:
    """External signal sealed the node; it never recorded its own envelope."""
    reason: str
    kind: Literal["abandon"] = "abandon"

@dataclass(frozen=True)
class TimeoutCause:
    """We waited for a late terminal envelope and the budget elapsed."""
    error: str = "wait-for-terminal-envelope budget elapsed"
    kind: Literal["timeout"] = "timeout"

Cause = Union[AbandonCause, TimeoutCause]   # discriminated union, matching state.py's style

def terminal_state_for(cause: Cause, *, prior: st.State,
                       session_id: Optional[str]) -> st.State:
    """The ONE map from cause → Abandoned/Timeout. Stamps the prior kind into
    the message exactly as the walkers do today."""

class NodeView(Protocol):
    """Uniform read/write view over ONE node, hiding Tree-shape vs dict-shape.
    Adapters live next to each shape; the core imports neither driver nor frames."""
    def state_kind(self) -> str: ...
    def session_id(self) -> Optional[str]: ...
    def state(self) -> st.State: ...
    def seal(self, new_state: st.State, *, error: str) -> None: ...
    def children(self) -> "list[NodeView]": ...

def seal_tree(roots: list[NodeView], cause: Cause) -> int:
    """Idempotently seal every eligible non-terminal node reachable from
    `roots` to the terminal state implied by `cause`. Returns count mutated.
    Pure: no clock, pid, file, or signal. Re-running is a no-op (sealed nodes
    are terminal). Never raises."""
```

Friendly factories (Design C ergonomics) keep the dominant case one line:

```python
abandoned = lambda detail: AbandonCause(detail)
timeout   = lambda detail=None: TimeoutCause() if detail is None else TimeoutCause(detail)
```

### Injected ports (the testability win — Design D)

```python
class Liveness(Protocol):
    """Decides whether the process owning a frame is gone. Folds os.kill +
    wall-clock TTL behind one method. MUST be the SAME port introduced in the
    merge-pipeline RFC (Candidate 2) — one liveness/clock seam per package."""
    def writer_is_dead(self, *, pid: Optional[int], started_at_iso: Optional[str]) -> bool: ...

class Clock(Protocol):
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...
```

Orphan reconciliation becomes a thin function over the port (no `os.kill`/`time` in its body):

```python
# frames.py
def reconcile_orphans(frames_by_key: dict[str, list[dict]], *, liveness: Liveness) -> int:
    total = 0
    for frames in frames_by_key.values():
        for frame in frames:
            pid = frame.get("writer_pid")
            if isinstance(pid, int) and liveness.writer_is_dead(
                pid=pid, started_at_iso=frame.get("started_at")
            ):
                total += seal_tree(_dict_node_views(frame),
                                   AbandonCause(f"writer pid {pid} is no longer alive"))
    return total
```

### Usage — the four trigger sites

```python
# 1. terminal_wait budget expiry — seal ONLY the straggler waiters (NOT the whole tree),
#    preserving exact current semantics. Envelope-recovery via state.step is untouched.
seal_tree([_TreeNodeView(w.node) for w in waiters], TimeoutCause())

# 2. reporter._emergency_finalize_on_shutdown (os.getpid stays at the trigger)
seal_tree([_TreeNodeView(n) for n in tree.nodes],
          AbandonCause(f"abandoned at shutdown (pid={os.getpid()})"))

# 3. reporter._finalize_own_frames (MCP boundary) + 4. frames orphan reconcile
seal_tree(_dict_node_views(frame), AbandonCause(reason))
# background._finalize_crashed inherits via report.finalize_own_frames — no edit at the crash site.
```

`_abandon_tree_nodes_in_place`, `mark_abandoned_in_dict_nodes`, the `_abandon_frame_nodes_in_place` alias, and `expire_to_timeout` are all deleted. **Two walkers → one.**

### What complexity it hides

The recursive walk (written once), the per-node eligibility gate, the `error` string construction + session_id fallback + "don't clobber existing error" rule, idempotency, the never-raise discipline, the `os.kill` ESRCH/EPERM semantics + ISO-8601 `started_at` parse + TTL comparison (behind `Liveness`), and the `time.monotonic`/`sleep` of the wait loop (behind `Clock`). Callers name a *cause*, not a target state — so the principled Timeout-vs-Abandoned choice cannot be set wrong at a call site.

## Dependency Strategy

**Mixed, by design:**

- **In-process pure core.** `sealing.py` imports only `state`. `seal_tree` mutates in-memory views the caller already holds; it does no I/O, locking, or process probing. The two shapes are unified behind `NodeView` via two ~10-line adapters living next to their shape (`_TreeNodeView` in driver.py, `_dict_node_views` in frames.py), chosen as zero-copy views rather than shape conversion (converting a live `Node` tree to dicts on a crash path is exactly the fragile allocation to avoid).
- **Ports & adapters for the impure triggers.** `Liveness` (PID + TTL) and `Clock` (monotonic + sleep) are injected. Production: `OsLiveness(ttl)` wrapping `os.kill`/`time` and the moved age computation; `SystemClock`. Tests: `FakeLiveness(dead_pids)`, `FakeClock`. The signal/atexit boundary stays in `shutdown.py`; the seal it performs routes through the pure core. Filesystem frame writes and the JSONL tail stay at the call sites (only the *clock* of the wait loop is injected — fully abstracting the JSONL source is a separate, larger change and the recovery path is deliberately kept distinct from sealing).

## Testing Strategy

**New boundary tests (pure, no PIDs, no files, no monkeypatching):**
- `seal_tree` over an in-memory mixed-state Tree: only eligible nodes sealed; `AwaitingUser` (parked) preserved; already-terminal untouched; `node.error` mirrors `state.error`; second call returns 0 (idempotent).
- `reconcile_orphans` with `FakeLiveness(dead_pids={…})`: dead-writer frames sealed to `abandoned` with session_id preserved; live-writer frames a no-op. **Replaces** `monkeypatch.setattr(frames, "_pid_alive", …)`.
- `OsLiveness` tested in isolation for the TTL / PID-reuse policy (known-live pid past the TTL → dead).
- `wait_for_terminal_signals` with `FakeClock`: budget-expiry seals stragglers to `Timeout` deterministically, no real `sleep`.
- `terminal_state_for`: correct mapping + message for each cause.

**Old tests to delete / migrate:**
- `tests/test_orphan_reconciliation.py` cases that monkeypatch `_pid_alive` / `read_orphan_ttl_seconds` → rewrite against `FakeLiveness`.
- Tests asserting the exact `"abandoned: "` message prefix — see behavior-change note below.
- Walker-specific unit tests for `_abandon_tree_nodes_in_place` / `mark_abandoned_in_dict_nodes` → one `seal_tree` boundary suite + two tiny adapter tests.

**Test environment needs:** none beyond in-memory fakes; `FakeLiveness`/`FakeClock` are pure.

## Implementation Recommendations

Durable guidance, decoupled from current file paths:

- **Define the seal operation once over a node abstraction, not per data shape.** Two representations (live objects, serialized dicts) are not a reason for two walkers — they're a reason for one walk over a `NodeView` both satisfy.
- **The terminal cause is chosen at the trigger and applied dumbly by the core.** Only the wait loop knows it *waited*; only reconciliation knows the *writer died*. Keep that semantic knowledge at the boundary; make the cause a typed value (a discriminated union, not a free string) so the core stays exhaustively checkable and a call site can't invent a third terminal kind ad hoc.
- **Inject the impure trigger inputs (PID-liveness, wall clock, monotonic clock) as ports** so the shared sealing logic is unit-testable with no real PIDs, files, or sleeps. Use the *same* `Liveness`/`Clock` port across the package (shared with the merge-pipeline work), not a second copy.
- **Seal the precise set the trigger means** (e.g. terminal-wait stragglers), not "the whole tree," unless a behavioral merge is explicitly intended and reviewed.
- **Keep the clean signal registry as-is.** It is already a deep module; this work changes only the body of the method it invokes.

### Cross-links

- **Composes with Candidate 1 (property-`Node`).** Once `Node.session_id`/`.error`/`.result` are read-through properties over `node.state`, `_TreeNodeView.seal` collapses from `node.state = new; _denormalize(node)` to a bare `node.state = new` and the `error=` argument becomes redundant. `NodeView.seal` is the seam that absorbs that migration — a one-line change inside one adapter, zero churn to the core or the dict adapter. Sequencing: land sealing first (it isolates every `.state` mutation behind `seal`), then the property refactor simplifies the Tree adapter and deletes `_denormalize`.
- **Shares Candidate 2's `Liveness` port.** The merge-pipeline RFC and this RFC must introduce the *same* liveness/clock seam.

### Behavior change to sign off (Rule 12)

Unifying the walkers means picking one message format. Recommend dropping the dict walker's `"abandoned: "` prefix (the state kind already says `abandoned`; matches the Tree-shape wording). This changes report `error` strings — any test asserting the exact text must update. This is the one non-mechanical behavior shift.

---

*Filed via the `improve-codebase-architecture` workflow (Candidate 3 of 5: node sealing / finalization). Recommendation is a hybrid of four independently-designed interfaces.*
