# Task 3 — Extract the Invocation factory from Caller

## Problem
`Caller` (in `agent_callstack/__init__.py`) carries ~95 LOC of setup helpers
(`_parent_project_cwd`, `_effective_cwd`, `_effective_log_dir`,
`_resolve_invocation_context`, and the child-env dict inside `_driver_for`)
that together decide the single most subtle thing in the package: **am I a
root invocation or nested inside a live one, and how does my identity
propagate to forked children?** That decision is computed inline in a method,
reads `os.environ` directly, and is only reachable for testing through the
whole `Caller` object. `mcp_server._invocation_identity` re-derives the same
nested-vs-root signal (now via the public `root_identity()` from Task 2) for
its own envelope/validation needs — two readers of one rule.

## Design — `agent_callstack/invocation.py :: InvocationFactory`

A small, frozen, **purely-config** value object that owns identity + env
propagation for one Caller's configuration. It reads process env *lazily* at
call time (never at construction) so a host that pops stale `CALLSTACK_ROOT_*`
between construction and use (mcp_server does exactly this) still gets the
corrected decision.

```python
@dataclass(frozen=True)
class InvocationFactory:
    explicit_cwd: Optional[str]
    explicit_log_dir: Optional[Path]
    explicit_invoke_id: Optional[str]
    max_depth: int

    def parent_project_cwd(self) -> Optional[str]
    def effective_cwd(self, parent_cwd: Optional[str]) -> str
    def effective_log_dir(self, cwd: str) -> Path
    def context(self, parent_cwd: Optional[str]) -> _InvocationContext   # nested-vs-root
    def child_env(self, ctx: _InvocationContext, *, depth_base: int) -> dict[str, str]
```

- `context()` is the lifted nested-vs-root branch verbatim: env carries a live
  root identity → reuse its `invoke_id`+`log_dir`, derive `frame_key`
  (CALLSTACK_FRAME_KEY → CLAUDE_CODE_SESSION_ID → most-recent-session → pid),
  mint a per-invocation `instance_id`; else mint a fresh root context.
- `child_env()` is the lifted env-stamping dict (DEPTH+1, ROOT_INVOKE_ID,
  ROOT_LOG_DIR, MAX_DEPTH). Deliberately omits the legacy
  `CALLSTACK_PARENT_SESSION` (the regression guard the invariant tests pin).
- Reads env through the public `env.py` helpers (`root_identity`,
  `frame_key`, `claude_code_session`, `in_nested_invocation`) — leans on
  Task 2's public surface rather than re-reading `os.environ` keys.

## Caller migration (body shrinks, seams preserved)
`Caller` builds one `InvocationFactory` from its config at `__init__`. The
helper methods become thin delegations so the existing behavioural seams keep
working unchanged:
- `_parent_project_cwd()` → `self._inv.parent_project_cwd()`
- `_resolve_invocation_context(parent)` → `self._inv.context(parent.cwd)`
- `_driver_for(...)` → uses `self._inv.effective_cwd` + `self._inv.child_env`,
  then assembles ChannelChannel+Driver (channel/trace/store wiring stays in
  Caller — it's runtime config, not identity).

`_effective_cwd` / `_effective_log_dir` are removed from Caller (callers go
through the factory).

## mcp_server
No behavioural change required (Task 2 already cleaned it). It keeps its own
`_invocation_identity` (validates the inherited dir exists, pops stale env,
mints for the envelope) because that is a *boundary-adapter* concern distinct
from building a context. It already uses the public `root_identity()`; the
factory uses the same primitive, so there is one rule, two consumers — no
duplicated logic. Verified it still imports only public names.

## Dependency category
**In-process** — pure config + env reads + path math. Directly unit-testable.

## Tests (replace-don't-layer + keep invariants)
- NEW `tests/test_invocation.py` — direct boundary tests for `InvocationFactory`:
  root vs nested `context()`; frame_key fallback chain; per-call distinct
  `instance_id`; `parent_project_cwd()` preferring `os.getcwd()` when nested
  (cross-project fresh) vs explicit cwd at root; `effective_log_dir` default
  vs explicit; `child_env` stamping (incl. the CALLSTACK_PARENT_SESSION
  omission and MAX_DEPTH propagation).
- KEEP `test_invariant_child_parent.py` / `test_api.py` — they exercise the
  same logic *through* the preserved `Caller._driver_for` /
  `_resolve_invocation_context` seams; these are valuable end-to-end invariants
  (grandchild forks from immediate parent), not shallow-module duplication.
  Left intact.

## Success criteria
- `python -m pytest tests/ -q` (clean env recipe) ≥ 329 passing.
- Caller's helper LOC drops sharply; the nested-vs-root rule lives in one
  directly-tested module.
