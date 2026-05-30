# RFC: Deepen invocation-identity resolution

_decide root-vs-nested ONCE as a pure function over a lazily-captured snapshot; delete the `os.environ` coordination hack; make `_InvocationContext` the sole path-layout authority_

Labels: enhancement

---

## Problem

Whether a `/call` is a fresh **root** or **nested** inside a live invocation is the single most subtle decision in the package (`invocation.py`'s own docstring says so). Today it is made **twice** and reconciled by a global side effect.

### The decision made twice, glued by env mutation (DRY-102)

1. **MCP boundary** — `mcp_server._resolve_invocation_identity(cwd)` (mcp_server.py:115) reads `root_identity()`, probes the filesystem (`root_dir_path.is_dir()` + `invocation_dir.is_dir()`), and on a stale-env miss **mutates `os.environ`** (pops `CALLSTACK_ROOT_*`, lines 155–156) *specifically so the second decision agrees with it*. Returns `(invoke_id, log_dir)`.
2. **The Caller** — `_build_caller(invoke_id, log_dir)` → `Caller.__init__` builds `InvocationFactory(explicit_invoke_id=…, explicit_log_dir=…)`; at call time `factory.context(parent.cwd)` (invocation.py:80) **re-reads `root_identity()` and re-decides** root-vs-nested from scratch.

The two readings are kept consistent only by the global `os.environ` mutation between them — a TOCTOU-prone coordination channel for a decision that should return a value.

### Explicit ids silently dropped when nested

When `factory.context()` decides "nested," it **never consults `explicit_invoke_id` / `explicit_log_dir`** (invocation.py:100–111) — the very values the MCP boundary just computed and passed into `Caller`. The public `Caller(invoke_id=…, log_dir=…)` parameters are silently overridden. There's a test pinning this as a *constraint*, not a documented contract.

### Path layout recomputed by hand in four places

`_InvocationContext` already derives every path (`invocation_dir = log_dir / invoke_id`, `report_path`, `frames_dir`, …). But not everyone goes through it:
- `mcp_server._report_path`: `log_dir / invoke_id / "report.yaml"`
- `reporter.py:550`: `Path(log_dir) / invoke_id`
- `channel.py:77`: `Path(root_dir) / invoke_id / "process_logs"`

Change the layout → hunt all four.

### Testability tax

Testing the decision requires `monkeypatch.setenv` + `os.chdir` + laying down real invocation dirs, and asserting on the `os.environ` *side effect* of the DRY-102 pop.

### What's actually clean (don't touch)

`_InvocationContext` is the right value object — keep it. `InvocationFactory`'s *lazy* env reading is a deliberate correctness feature (an async host may correct stale `CALLSTACK_ROOT_*` between Caller construction and invocation) — keep the laziness. `_resolve_cwd`'s sensitive-prefix / `resolve(strict=True)` gating is user-input *security* validation, a separate concern — leave it in `mcp_server`.

## Proposed Interface

A **C+D hybrid** of four independently-designed interfaces that *converged* on: collapse to one decision that returns a value, delete the env mutation, make `_InvocationContext` the sole layout authority, and inject the impure inputs. The hybrid takes Design D's pure-function-over-a-lazily-captured-snapshot, Design C's decide-once-and-consume-at-the-boundary + warning-as-data, and a light DI seam (rejecting Design B's source/policy/claim chain as YAGNI — its own author recommended trimming it).

### The pure decision

```python
@dataclass(frozen=True)
class IdentityInputs:
    """Everything the decision consumes, captured as a snapshot at decision
    time. Pure: no method touches os.environ / the filesystem / the clock."""
    explicit_cwd: Optional[str]
    explicit_log_dir: Optional[Path]
    explicit_invoke_id: Optional[str]
    parent_cwd: Optional[str]
    root: Optional[tuple[str, str]]       # env.root_identity() — (invoke_id, log_dir) or None
    frame_key: Optional[str]              # CALLSTACK_FRAME_KEY
    claude_code_session: Optional[str]
    process_cwd: str                      # os.getcwd()
    process_pid: int                      # os.getpid()

@dataclass(frozen=True)
class ResolvedIdentity:
    """The authoritative outcome of the ONE decision. Carries the value-object
    context plus what the boundary needs synchronously, and any advisory the
    core would otherwise have printed (warning-as-data, not stderr I/O)."""
    context: _InvocationContext
    max_depth: int
    warning: Optional[str] = None         # stale-env rejection, or explicit-ignored-when-nested

    @property
    def report_path(self) -> Path:
        return self.context.report_path

def resolve_identity(inp: IdentityInputs, *, dir_exists: Callable[[Path], bool]) -> ResolvedIdentity:
    """THE decision, made once. Pure given `inp` + the injected `dir_exists`
    probe:
      - root present AND dir_exists(log_dir/invoke_id)  -> NESTED
      - root present BUT dir missing (stale env)        -> FRESH (+ warning), NO env mutation
      - root absent                                     -> FRESH
    Honors explicit_* only on the FRESH branch; if explicit_* were supplied
    AND the decision is NESTED, records that in `warning` instead of silently
    dropping them."""
```

### The factory captures lazily, at call time (laziness preserved, decision made once)

```python
@dataclass(frozen=True)
class InvocationFactory:
    explicit_cwd: Optional[str]
    explicit_log_dir: Optional[Path]
    explicit_invoke_id: Optional[str]
    max_depth: int
    env_snapshot: Callable[[], IdentitySnapshot] = _os_snapshot   # reads env+cwd+pid NOW
    dir_exists: Callable[[Path], bool] = _os_dir_exists

    def resolve(self, parent_cwd: Optional[str]) -> ResolvedIdentity:
        # Snapshot is taken HERE, at decision time — so an async host popping
        # stale CALLSTACK_ROOT_* between construction and invoke is honored.
        inp = self._capture(parent_cwd)        # the ONLY place env/cwd is read
        return resolve_identity(inp, dir_exists=self.dir_exists)
```

### Usage — the double decision collapses, the env hack dies

```python
# mcp_server.call() — AFTER. No identity computation, no os.environ.pop, no _report_path.
caller = Caller(session=session_id or None, model=model or None,
                cwd=resolved_cwd or None, timeout=timeout)
ident = caller.resolve_identity(parent_cwd=os.getcwd())   # the ONE decision
if ident.warning:
    print(f"[callstack] WARN: {ident.warning}", file=sys.stderr)   # boundary owns I/O
report_path = str(ident.report_path)        # layout from _InvocationContext
# caller.call_many reuses `ident.context` verbatim — no second decision.
```

The stale-env case: `resolve_identity` sees `root` present but `dir_exists(...)` False → returns a FRESH context with a `warning`. The Caller's runtime uses the *same* `ResolvedIdentity` object — there is no second reader of `os.environ` to keep in sync, so the `pop` is deleted outright.

### `_InvocationContext` becomes the sole layout authority

Delete the four hand-rolled `{log_dir}/{invoke_id}/…` joins. `mcp_server`/`reporter` read `ctx.report_path`/`ctx.invocation_dir`. `channel.py` (which runs from raw env in CLI/library use and legitimately has no `Caller`) constructs an `_InvocationContext(invoke_id, Path(root_dir), …)` and reads `.invocation_dir` rather than re-spelling the join — so the path grammar lives in exactly one place.

### What complexity it hides

The root-vs-nested branch, the `effective_cwd` precedence (`explicit > parent > getcwd`), the stale-env validation (now a pure branch over `dir_exists`, not a mutation), the frame-key fallback chain (`frame_key → claude_session → most_recent_session(cwd) → pid-N`), `instance_id` minting for sibling invokes, and `child_env` propagation — all behind one `resolve()` returning one value.

## Dependency Strategy

**Category: Local-substitutable, with a light injected seam.** The three impure inputs (env, filesystem dir-existence, cwd/pid) are captured into the frozen `IdentityInputs` snapshot via one `env_snapshot` callable, and the one filesystem question is a `dir_exists` callable. Production wraps `os.environ`/`os.getcwd`/`Path.is_dir`; tests pass plain fakes.

- **Lazy but once:** the snapshot is taken inside `factory.resolve()` at decision time (lazy — reflects env as-of-call, honoring the async-host stale-pop concern), then frozen into one `ResolvedIdentity` reused by every consumer (once — no second reader).
- **Snapshot coherence (bonus):** reading env+fs+cwd into one snapshot closes the TOCTOU window the old two-reads-of-`os.environ` design had.
- **Deliberately *not* three separate protocols.** Design B's `IdentitySource`/`ValidationPolicy`/`FrameKeyResolver` chain and Design D's `EnvView`/`DirProbe`/`Process` trio are more machinery than four invocation kinds and one production precedence justify; one snapshot callable + one `dir_exists` callable captures the testability win at a fraction of the surface.

## Testing Strategy

**New boundary tests (pure, no `setenv`/`chdir`/real dirs):**
- "Nested under a live root": `IdentityInputs(root=("id","/log"), …)` + `dir_exists=lambda p: True` → `ctx.is_nested`, inherits id/log_dir, `frame_key` from env, non-empty `instance_id`.
- "Stale env → fresh root": `root=("STALE","/dead")` + `dir_exists=lambda p: False` → `ctx.is_nested is False`, minted fresh id, `warning` set, **and `os.environ` untouched**.
- "Explicit id while nested": explicit ids supplied + nested decision → ids not used, `warning` records the override (the silent-drop made visible).
- Root with explicit ids honored on the fresh branch.
- `effective_cwd` precedence and the frame-key fallback ladder, each with injected fakes.

**Old tests to delete / migrate:**
- `tests/test_invocation.py` / `tests/test_lifecycle_hardening.py` cases asserting on the DRY-102 `os.environ` pop and on stderr capture → rewrite as assertions on `ResolvedIdentity` (`is_nested`, `invoke_id`, `warning`).
- The "explicit_invoke_id_is_ignored_when_nested" constraint test → becomes a `warning`-surfacing assertion.

**Test environment needs:** none — plain in-memory callables.

## Implementation Recommendations

Durable guidance, decoupled from current file paths:

- **Decide identity once and return it as a value.** A subtle decision that two components must agree on should be a pure function returning a value both consume — never two readings of global state reconciled by mutating that state. The `os.environ` pop is a coordination hack that disappears the moment there's a single decision.
- **Capture the impure inputs as a snapshot at decision time, then freeze.** This preserves the lazy-env contract (decision reflects env at call time) while guaranteeing one coherent read — closing the read-twice race.
- **Surface degraded/overridden decisions as data, not I/O and not silence.** Stale-env rejection and "explicit ids ignored because nested" should be fields on the result the boundary can log — pure core, visible contract, no more test-pinned silent behavior.
- **One value object owns the path layout.** Every `{log_dir}/{invoke_id}/…` join goes through it; no component re-spells the grammar. Even the raw-env CLI path should construct the value object rather than recompute.
- **Keep DI light.** Inject the genuinely-impure, genuinely-hard-to-test inputs (env, dir-existence) as one snapshot + one probe; do not proliferate ports for `getpid`/`getcwd` or port-ify the other already-pure `env.py` readers.
- **Out of scope:** user-input `cwd` security gating (`_resolve_cwd`) is a different responsibility — leave it at the boundary.

### Cross-links

- **Shares the filesystem seam of Candidates 2 and 3.** The `dir_exists` probe and `most_recent_session` directory scan overlap with the merge-pipeline `FileStore` and the `Liveness` port. These should resolve to **one filesystem/clock abstraction for the package**, not three parallel seams.
- This is the natural place to finally make "explicit id ignored when nested" a *surfaced* outcome rather than a silent, test-pinned constraint.

---

*Filed via the `improve-codebase-architecture` workflow (Candidate 4 of 5: invocation-identity resolution). Recommendation is a hybrid of four independently-designed interfaces.*
