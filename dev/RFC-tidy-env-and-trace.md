# RFC: Tidy `env.py` readers and split `trace.py`

_low-priority cohesion cleanups: collapse the repeated numeric-reader clamp/default pattern into one declarative table; separate `TreeStore` from `TraceWriter`_

Labels: enhancement

---

> **Priority note.** Unlike the other four RFCs from this architecture pass, this one fixes **no correctness or testability gap** — it is tidiness. Both `env.py` and `trace.py` are already correct, documented, and tested. Do this last, or skip it under time pressure. It is filed for completeness and because the `env.py` policy table is a natural neighbor to the shared config/seam that Candidates 2–4 may introduce.

## Problem

Two small, independent cohesion issues — deliberately *not* worth the heavyweight multi-interface design treatment the other candidates got.

### 1. `env.py` — seven numeric readers repeat one shape

`env.py` is a well-built single source of truth (DRY-101): every `CALLSTACK_*` name lives here with a typed reader carrying default + clamp policy. The shallowness is narrow: seven numeric readers repeat the identical structure —

> `os.environ.get(NAME)` → `None` ⇒ default → `int`/`float` parse (`ValueError` ⇒ default) → reject `≤0` / `<0` ⇒ default → clamp to ceiling ⇒ else value

— across `max_depth` (133), `max_fanout` (147), `max_background` (159), `report_debounce_secs` (172), `read_finalize_wait_seconds` (185), `read_orphan_ttl_seconds` (202), `sync_budget_secs` (222). Adding a knob means a ~12-line copy-paste plus a copy-pasted 4-method test class. A bug fixed in one reader's clamp logic isn't fixed in the others.

The string readers (`frame_key`, `own_session`, `claude_code_session`, `root_identity`) have no shared shape worth collapsing and stay as-is.

### 2. `trace.py` — two unrelated concepts in one file

`trace.py` bundles `TraceWriter` (append one JSONL line per turn, continuous, single-process) and `TreeStore` (snapshot the execution tree to a sidecar on yield, load once on resume). They share no state, no helpers, and orthogonal lifetimes. They already test in separate classes (`TestTraceWriter`, `TestTreeStore`). A reader looking for "how do trees persist?" finds `TraceWriter` first and must skip past it.

## Proposed Interface

### 1. `env.py` — one declarative spec table + one generic numeric reader

```python
@dataclass(frozen=True)
class _NumericKnob:
    name: str
    parse: Callable[[str], float]     # int or float
    default: float
    min_value: Optional[float] = None     # values <= or < this fall back to default
    max_value: Optional[float] = None     # clamp ceiling
    reject_at: Literal["<=", "<"] = "<="  # max_depth/fanout/background reject <=0; the rest reject <0

# The table IS the documentation — keep each knob's rationale as a comment here.
_MAX_DEPTH = _NumericKnob(ENV_MAX_DEPTH, int, 10, min_value=0, max_value=32, reject_at="<=")
_MAX_FANOUT = _NumericKnob(ENV_MAX_FANOUT, int, 64, min_value=0, reject_at="<=")
# ... one entry per knob ...

def _read_numeric(k: _NumericKnob) -> float:
    """The single clamp/default policy every numeric knob shares."""
    raw = os.environ.get(k.name)
    if raw is None:
        return k.default
    try:
        v = k.parse(raw)
    except ValueError:
        return k.default
    if k.reject_at == "<=" and v <= 0: return k.default
    if k.reject_at == "<"  and v < 0:  return k.default
    return min(v, k.max_value) if k.max_value is not None else v

# Public readers stay as thin named wrappers (preserve the public API + int/float return types):
def max_depth() -> int:        return int(_read_numeric(_MAX_DEPTH))
def read_orphan_ttl_seconds() -> float: return _read_numeric(_ORPHAN_TTL)
# ...
```

The public function names and signatures are unchanged — only the bodies collapse to the shared reader. The string readers and the `ENV_*` name constants stay exactly as they are.

**Rejected alternative — a resolved `Config` object.** A frozen config dataclass resolved once would fight the deliberate *lazy* env reads that Candidates 1 and 4 depend on (env can change between Caller construction and call time), and it is strictly more machinery for no gain. Keep per-call reads.

### 2. `trace.py` — move `TreeStore` to its own module

`TraceWriter` stays in `trace.py`. `TreeStore` (plus the `_json_default` helper it uses) moves to a new `tree_store.py`. Re-export `TreeStore` from `trace.py` for one release so any external `from agent_callstack.trace import TreeStore` keeps working, then drop the alias.

## Dependency Strategy

**Category: In-process.** Both changes are pure in-memory/local — no external dependencies, no new seams. `env.py` reads `os.environ` (unchanged); `trace.py`/`tree_store.py` do local file I/O (unchanged). Merge directly; test at the existing boundaries.

## Testing Strategy

- **`env.py`:** replace the seven per-reader test classes with one parametrized test over the knob table (unset → default; valid → value; non-numeric → default; out-of-range → clamp/default; boundary at `0`). Keep one explicit test per knob for its *specific* default/ceiling values so a wrong table entry is caught. Net: less duplicated test code, same coverage.
- **`trace.py` / `tree_store.py`:** the existing `TestTraceWriter` / `TestTreeStore` split moves with the code — `TestTreeStore` targets `tree_store`. Add one import-compatibility test asserting the `trace.TreeStore` re-export resolves during the deprecation window.

## Implementation Recommendations

- **Single-source the *policy*, not just the names.** `env.py` already single-sources the variable *names*; the clamp/default/reject *policy* should be single-sourced too, so it can't drift between knobs.
- **Keep the readers' public names and return types.** Callers import `max_depth()` etc.; the table is an implementation detail behind unchanged signatures.
- **One concept per module.** Two types that share no state and have orthogonal lifetimes belong in separate files even when both are "persistence."
- **Do not build a resolved config object.** Lazy per-call env reads are a deliberate, load-bearing property elsewhere in the package.
- **Sequence last.** This RFC fixes no correctness or testability gap; land it after (or alongside) the substantive deepenings, and only if cheap. If Candidates 2–4 introduce a shared config/seam, fold the numeric-knob policy in there rather than filing it separately.

---

*Filed via the `improve-codebase-architecture` workflow (Candidate 5 of 5: `env.py` readers + `trace.py` cohesion). Deliberately right-sized: these are minor cleanups, not deep-module refactors, and were handled with a direct recommendation rather than parallel interface design.*
