# RFC: Deepen the turn-execution seam

_extract `TurnExecutor`, single-source-of-truth `Node`, port timing contract_

Labels: enhancement

---

## Problem

The pure state machine in `state.py` (`step(state, event) -> (state, [effects])`) is a model deep module — exhaustive, side-effect-free, trivially testable. But that purity was bought by pushing all of the messy integration into the driver's impure half, and that half has become the codebase's deepest architectural friction.

### Shallow / leaky: `driver._run_turn` (driver.py:550–691)

A single 141-line method turns one `RunTurn` effect into a state `Event` while owning **six** unrelated concerns:

1. Timing (`t0`, `node.duration += …`)
2. Early-session-id propagation — the `_early_session` closure (driver.py:576–584) that **mutates `node.session_id` and `node.state` directly, outside the `step()`/`_denormalize` loop**
3. Calling `channel.run_turn(...)`
4. Exception → `TurnFailed` translation (`TurnTimeout` and generic `Exception` arms)
5. Three separate `self.trace.write(...)` call sites (timeout / generic-error / success)
6. Clone-path resolution (`resolve_session`, filesystem I/O) + envelope parse + upstream-error classification

Callers cannot predict what state `node` will be in after `_run_turn` returns.

### Two sources of truth

`Node` (driver.py:88) denormalizes `session_id`, `result`, `summary`, `error`, etc. off the pure `State`, requiring a `_denormalize(node)` call after every `step()` (driver.py:528). The `_early_session` callback writes to *both* `node.session_id` and `node.state` without routing through `step()`/`_denormalize`, so during a long first turn the mirror can be inconsistent — and `Node.to_dict()` (driver.py:139) can serialize a `state` dict and a flat field that disagree. There is no test for this desync.

### The test substitute lies about timing

A `Channel` Protocol already exists (channel.py:163) — a real port. But the production and test adapters **fire the mid-turn callback at different times**:

- `ClaudeChannel` fires `on_session_id` *mid-turn*, from the reader thread, on the `system init` message — while the node is still `AwaitingTurn(session_id=None)`.
- `ScriptedChannel` fires `on_session_id` *after* the turn completes (testing.py:69–74).

Because the timing differs, the early-session propagation path, the `node.state`/`node.session_id` desync race, and the `resolve_session() is None` clone-path fallback (driver.py:653–656) are **all unreachable from the test suite**. The pure `step()` is heavily tested; the place bugs actually live is not.

## Proposed Interface

A **C+D hybrid** (of four designs explored): a property-based `Node` for single-source-of-truth, a deep `TurnExecutor` scoped to the turn, and a channel port whose timing contract can't drift from its double.

### 1. `Node` single source of truth (eliminate the desync by construction)

Delete `_denormalize`. Make the denormalized fields read-through properties over `state`, so there is no second field to drift:

```python
@dataclass
class Node:
    id: str
    task: str
    state: st.State
    parent_lines: int = 0
    duration: float = 0.0
    children: list["Node"] = field(default_factory=list)
    max_context_tokens_seen: int = 0
    call_type: str = "fork"
    clone_path: Optional[str] = None   # genuinely NOT derivable from state — stays a field

    @property
    def session_id(self) -> Optional[str]:
        return getattr(self.state, "session_id", None)
    @property
    def result(self) -> Any:
        return self.state.result if isinstance(self.state, st.Done) else None
    @property
    def error(self) -> Optional[str]:
        return getattr(self.state, "error", None)   # Failed/Timeout/Abandoned
    @property
    def summary(self) -> Optional[str]:
        return self.state.summary if isinstance(self.state, st.Done) else None
    @property
    def suggested_next(self) -> Optional[str]:
        return self.state.suggested_next if isinstance(self.state, st.Done) else None
```

Early-session-id propagation then becomes a single write — re-assign `node.state = AwaitingTurn(session_id=sid)` — and the derived `session_id` updates with it. The "two writes must agree" hazard collapses.

### 2. Deep `TurnExecutor`, scoped to `RunTurn`

```python
@dataclass(frozen=True)
class TurnOutcome:
    event: st.Event                 # TurnCompleted | TurnFailed, fed straight into step()
    session_id: Optional[str]
    clone_path: Optional[str]       # None is a first-class outcome
    result: Optional[TurnResult]    # carries tokens/duration for the trace; None on failure

class TurnExecutor:
    def __init__(self, transport: TurnTransport, resolver: SessionResolver,
                 *, cwd: Optional[str], timeout: int) -> None: ...

    def execute(self, effect: st.RunTurn, *, node_session_id: Optional[str],
                frame_key: str,
                on_session_id: Optional[Callable[[str], None]] = None) -> TurnOutcome: ...
```

`SpawnChild` stays in the driver — the test gap is entirely on the turn path, so a uniform `perform` + a `ChildRunner` recursion port would add indirection without buying coverage.

Driver `_run_turn` collapses to: build the node-bound `on_session_id` closure (still the driver's, since it touches `node.state`), call `executor.execute(...)`, write the trace from `outcome.result`+timing, set `node.clone_path = outcome.clone_path`, return `outcome.event`.

### 3. Channel port contract + shared conformance test

Pin the firing timing in the port's type/docstring instead of leaving it implementation-defined:

```python
class TurnObserver(Protocol):
    def on_session_id(self, session_id: str) -> None:
        """Fired EXACTLY ONCE, the first time a session id is observed on the
        wire, WHILE THE TURN IS STILL IN FLIGHT — before the transport has the
        final TurnResult. Advisory: a raise is logged, never propagated."""
```

### Usage (driver loop, after)

```python
def _run_turn(self, effect, node, depth, parent_file) -> st.Event:
    def early_session(sid: str) -> None:        # the only Node mutation, driver-owned
        if isinstance(node.state, st.AwaitingTurn) and node.state.session_id != sid:
            node.state = st.AwaitingTurn(session_id=sid)   # derived session_id follows
            self._notify()
    outcome = self.executor.execute(
        effect, node_session_id=node.session_id, frame_key=node.id,
        on_session_id=early_session,
    )
    node.duration += outcome.result.duration if outcome.result else 0.0
    if outcome.clone_path is not None:
        node.clone_path = outcome.clone_path
    self._write_trace(node, depth, outcome)
    return outcome.event
```

### What complexity it hides

All six concerns (timing, observer wiring + once-only/advisory-raise semantics, the `channel.run_turn` call incl. UUID pre-allocation and `CALLSTACK_FRAME_KEY`, exception→`TurnFailed` mapping, the `None`-clone-path rule, and `parse_envelope` + `_classify_upstream_failure`) move behind `TurnExecutor.execute`. `step()` is byte-for-byte unchanged. The driver keeps only what is genuinely tree-aware: the `node.state` early-session mutation, the trace write, and `clone_path`.

## Dependency Strategy

**Category: Ports & Adapters** — the `claude` subprocess and the session-resolution filesystem read are the two cross-boundary dependencies.

- **`TurnTransport`** (port; evolves the existing `Channel` Protocol at channel.py:163). Production adapter: `ClaudeChannel`, firing `on_session_id` mid-turn from the reader loop. Test adapter: a `FakeTurnTransport` scripted as a *sequence of wire events* with `emits_session_id_at` defaulting to `"init"` (mid-turn), so new tests get production-faithful timing for free; the legacy fire-at-end behavior is an explicit, named opt-in.
- **`SessionResolver`** (port; `SessionLocator.resolve` already satisfies it structurally). `None` is a first-class degraded outcome. Test adapter: `lambda sid, cwd=None: None` for the fallback, or a dict-backed fake for the hit case.

The drift exists today only because the `on_session_id` contract was "we call it at some point." Closing it three ways: (1) timing is part of the typed contract; (2) the test adapter's default matches production; (3) a single parametrized **conformance test** runs the same assertions (called exactly once; called before `run_turn` returned) against *both* `ClaudeChannel` and the fake, so an "optimization" that fires the fake late fails the test.

## Testing Strategy

**New boundary tests (at the `TurnExecutor` + port boundary):**
- Mid-turn `on_session_id` lands while the node is still `AwaitingTurn` and rewrites it to `AwaitingTurn(session_id=…)` — the desync the closure exists to fix, finally observable because the fake fires mid-turn.
- `resolve_session` returns `None` → `outcome.clone_path is None`, turn still completes (the untested fallback at driver.py:653–656).
- Timeout and generic-exception arms each produce the right `TurnFailed` + trace line.
- Unparseable / upstream-rate-limit text → typed `TurnFailed` via the classifier.
- A `Node` `to_dict`/`from_dict` round-trip can no longer encode disagreeing `state` vs flat fields (the flat fields are gone).
- **Conformance test** parametrized over `[ClaudeChannel (gated on a real binary), FakeTurnTransport]`: `on_session_id` fired exactly once, before return.

**Old tests to delete / replace:**
- `ScriptedChannel`-based driver tests that implicitly relied on fire-at-end timing — replace with `FakeTurnTransport` sequences (keep a thin `ScriptedChannel` shim short-term to avoid churning ~20 call sites, then migrate).
- Any test asserting on `_denormalize` behavior or on flat `Node` fields as independent state.

**Test environment needs:** no new infrastructure — both ports have pure in-memory adapters. The production arm of the conformance test is gated on a real `claude` binary (CI without one runs only the fake arm); a recorded-NDJSON replay adapter is a good follow-up to close that gap.

## Implementation Recommendations

Durable guidance, decoupled from current file paths:

- **The state machine stays pure and is the source of truth for a node's state.** Never re-introduce denormalized mirrors of state fields; derive them. `clone_path` is the one legitimate non-derived field (it's a filesystem fact, not a state fact).
- **A turn-execution module should own the entire imperative shell of one LLM turn** — timing, transport invocation, mid-turn event propagation, failure mapping, tracing inputs, envelope parsing/classification — and expose a single `execute(effect) -> outcome` entry point that returns data, not mutations. It must not know about `Tree` topology or child spawning.
- **Cross-boundary dependencies are ports with explicit timing contracts.** When a callback's *timing* is behaviorally significant, encode it in the port contract and enforce it with a conformance test shared by every adapter — a callback whose firing time is "implementation-defined" is how the production/test drift happened.
- **Migration gate (must verify before deleting `_denormalize`):** audit every external writer of `node.session_id` / `node.error` / `node.result` — notably orphan reconciliation in `frames.py` and shutdown sealing in `reporter.py` (which build `Abandoned`/`Timeout` states). Each must set `node.state`, not a flat field. With properties, a stray flat-field write becomes a loud failure (good), but the call sites must migrate first.
- **Deferred (YAGNI):** a first-class multi-event turn stream (partial output, incremental usage) and a pluggable envelope-classification policy are attractive but speculative — the current subprocess port cannot emit streaming events, and there is only one error policy. Add them when a concrete consumer appears.

---

*Filed via the `improve-codebase-architecture` workflow (Candidate 1 of 5: the turn-execution seam). Recommendation is a hybrid of four independently-designed interfaces.*
