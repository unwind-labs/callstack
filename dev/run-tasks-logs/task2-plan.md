# Task 2 — Clean the MCP boundary

## Problem
`plugins/callstack/mcp_server.py` (the FastMCP shim) reaches into private /
internal names of `agent_callstack`:

- `from agent_callstack import _new_invoke_id`  (underscore = private)
- `import agent_callstack.env as _env` then `_env.max_fanout/_env.max_background/
  _env.root_identity/_env.ENV_ROOT_INVOKE_ID/_env.ENV_ROOT_LOG_DIR`  (internal module)
- inline `from agent_callstack.session import SessionLocator`  (submodule reach-in)

Worse, the **background-runs lifecycle** — a first-class runtime capability —
lives entirely inside the shim: the module-level `_background_tasks` dict, the
`asyncio.create_task`/`to_thread` scheduling, `_reap_finished_background_tasks`,
the `asyncio.shield`-on-await, the concurrency cap, and force-finalize on
exception. None of it is testable without the FastMCP entrypoints + JSON parsing.

## Design — lift the lifecycle into `agent_callstack.background.BackgroundRuns`

A deep, asyncio-aware registry that owns the full background lifecycle and
returns typed *outcome* values. The shim becomes a pure adapter: validate →
delegate → format JSON. The package owns logic; the shim owns the wire format
(a ports-&-adapters split — the package never learns the JSON envelope shape).

```python
# agent_callstack/background.py
class _CallerLike(Protocol):
    def call_many(self, tasks, *, context="fork") -> MultiResult: ...

# start() outcomes
@dataclass Started(invoke_id, report_path)
@dataclass CapReached(cap, outstanding)
# reconcile() outcomes
@dataclass Pending(invoke_id, report_path)
@dataclass Done(invoke_id, report_path, result: MultiResult)
@dataclass Crashed(invoke_id, report_path, error: str)
@dataclass NotFound(invoke_id)

class BackgroundRuns:
    def __init__(self, *, max_outstanding: Callable[[], int] | None = None): ...
        # default provider = env.max_background, read per-start so env changes
        # (and per-test monkeypatch of CALLSTACK_MAX_BACKGROUND) are honored.
    def start(self, *, invoke_id, caller, tasks, context,
              report_path, log_dir) -> Started | CapReached:
        # reap finished-unreconciled, enforce cap, schedule on a worker thread.
    async def reconcile(self, invoke_id, *, timeout) -> Pending|Done|Crashed|NotFound:
        # shield+wait_for; timeout => Pending (kept); exception => pop + finalize
        # own frames + Crashed; success => pop + Done.
    def reap(self) -> None
    def clear(self) -> None                 # test teardown
    def task_for(self, invoke_id) -> asyncio.Task | None   # advanced/test access
    def __contains__(self, invoke_id) -> bool
    def __len__(self) -> int
```

`reconcile`'s Crashed path builds `InvocationReport(invoke_id, log_dir, cwd="")`
and calls `finalize_own_frames` (the public Task-1 boundary), swallowing/logging
errors — so the lifted code keeps using only the public report facade.

## Public-name gaps closed in `agent_callstack/__init__.py`
- add public `new_invoke_id()` (wraps `_new_invoke_id`; keep private for internals)
- re-export `max_fanout`, `max_background`, `root_identity` from `env`
- `ENV_ROOT_INVOKE_ID` / `ENV_ROOT_LOG_DIR` already re-exported — reuse
- promote `SessionLocator` to `__all__` (already imported into the package ns)
- export `BackgroundRuns` + the six outcome types; add `BackgroundRuns`,
  `SessionLocator`, `new_invoke_id` to `__all__`

## mcp_server.py migration (depends ONLY on public `agent_callstack` names)
- drop `import agent_callstack.env as _env`; drop `_new_invoke_id` private import
- top-level `from agent_callstack import (..., BackgroundRuns, SessionLocator,
  new_invoke_id, max_fanout, root_identity, ENV_ROOT_INVOKE_ID, ENV_ROOT_LOG_DIR)`
- module-level `_background = BackgroundRuns()` replaces `_background_tasks` dict
- `call(run_in_background=True)` → `_background.start(...)`, match Started/CapReached
- `await_call` → `_background.reconcile(...)`, match Pending/Done/Crashed/NotFound
- `_invocation_identity` keeps living here (overlaps Task 3 — leave it) but uses
  the public `root_identity` / `new_invoke_id` / env constants
- `_max_fanout` test wrapper now calls public `max_fanout`
- sync-`call` exception path keeps `_finalize_at_boundary` (thin wrapper over the
  public `InvocationReport.finalize_own_frames` — already public after Task 1)

## Dependency category
**In-process** — the lifted lifecycle is asyncio + an in-memory dict; no new
external deps. `background.py` is the package's async adapter; the synchronous
core (Caller/Driver/channel) stays asyncio-free.

## Tests (replace, don't layer)
- NEW `tests/test_background.py` — boundary tests for `BackgroundRuns` with a
  stub `_CallerLike`: start→Started, cap→CapReached, reconcile timeout→Pending
  (kept), success→Done (popped), crash→Crashed (popped + finalize called),
  unknown→NotFound, reap drops finished-unreconciled (deterministic via
  `task_for`). No FastMCP, no JSON.
- UPDATE `tests/test_mcp_server.py` — background/reap tests retarget
  `_background_tasks` dict → `_background` object API (`in`, `len`, `task_for`,
  `clear`). Keep every existing intent (started-immediately, full-envelope-when-
  done, pending-on-timeout, unknown-id-error, validation-still-sync,
  exception-surfaced, cap-rejects, reaped). The autouse fixture clears
  `_background` instead of the dict.
- Net test count must not drop; new file adds tests.

## Verify
`env -u CALLSTACK_ROOT_INVOKE_ID -u CALLSTACK_ROOT_LOG_DIR -u CALLSTACK_DEPTH \
   -u CALLSTACK_FRAME_KEY -u CALLSTACK_OWN_SESSION -u CALLSTACK_MAX_DEPTH \
   -u CLAUDE_CODE_SESSION_ID -u CLAUDE_SESSION_ID python -m pytest tests/ -q`
=> 320 + new, all green.
