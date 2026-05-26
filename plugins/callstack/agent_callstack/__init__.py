"""agent_callstack — fork an agent, get its result back.

Public API:

    from agent_callstack import call, call_many, resume, Caller, Result

    result = call("Implement the auth module")
    print(result.value)

    try:
        out = call("Process refund for order 123")
    except CallYielded as y:
        out = resume(y.token, ask_user(y.question))

For power users (custom session, model, permission handler, etc.):

    caller = Caller(model="opus", timeout=600)
    r = caller.call("...")
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Sequence

from .background import (
    BackgroundRuns,
    CapReached,
    Crashed,
    Done,
    NotFound,
    Pending,
    Started,
)
from .channel import ClaudeChannel, PermissionHandler, allow_all, shutdown_pool
from .driver import Driver, Node, Tree
from .frames import (
    _build_merged_report,
    _frames_cache_clear,
    _load_frames,
)
from .invocation import InvocationFactory
from .invocation_ctx import _InvocationContext, _new_invoke_id, _utc_now_iso
from .report import InvocationReport, ROOT_FRAME_KEY
from .reporter import (
    _DEFAULT_REPORT_DEBOUNCE_SECS,
    _LiveReporter,
    _atomic_write_bytes,
    _atomic_yaml_write,
    _interprocess_lock,
    _report_debounce_secs,
)
from .results import (
    CallFailed,
    CallYielded,
    MultiResult,
    Result,
    YieldToken,
    _result_from_node,
    _results_from_tree,
    _unwrap_single,
)
from .session import PROJECTS_DIR, SessionLocator, SessionRef, encode_project_dir
from .shutdown import install_shutdown_hooks as _install_shutdown_hooks
from .terminal_wait import wait_for_terminal_signals
from .trace import TraceWriter, TreeStore


# REVIEW-202: install shutdown hooks at process startup, on the main
# thread. Doing this at import time guarantees `signal.signal()` runs
# from the thread Python booted on — the constructor-side install used
# to run from `asyncio.to_thread` workers and silently skipped signal
# registration, leaving fix #3 a partial no-op in the MCP server.
# Idempotent on re-import / re-call; no-op when already installed.
_install_shutdown_hooks()


__all__ = [
    "call", "call_many", "resume",
    "Caller", "Result", "YieldToken",
    "CallYielded", "CallFailed", "MultiResult",
    "InvocationReport", "ROOT_FRAME_KEY",
    # Background-run lifecycle (used by async hosts like the MCP server).
    "BackgroundRuns",
    "Started", "CapReached", "Pending", "Done", "Crashed", "NotFound",
    # Boundary helpers an MCP/host adapter legitimately needs.
    "SessionLocator", "new_invoke_id",
    "max_fanout", "max_background", "root_identity",
]


def new_invoke_id() -> str:
    """Mint a fresh, sortable invocation id (`YYYYMMDDTHHMMSS-<8 hex>`).

    Public spelling of the internal `_new_invoke_id`. Async hosts (the MCP
    server) need to mint an id before launching an invocation so they can
    return `report_path` synchronously; this is the supported entry point."""
    return _new_invoke_id()


# ---------- Env constants (re-exported from env.py for compatibility) ----------

# DRY-101: the constants and the parsing policy live in `env.py`.
# Re-exported here so external consumers (mcp_server, tests, downstream
# code) continue importing from `agent_callstack` without churn.
from .env import (  # noqa: E402
    ENV_DEPTH,
    ENV_ROOT_INVOKE_ID,
    ENV_ROOT_LOG_DIR,
    ENV_FRAME_KEY,
    ENV_OWN_SESSION,
    ENV_CLAUDE_SESSION,
    ENV_MAX_DEPTH,
    read_finalize_wait_seconds,
)
from .env import max_depth as _default_max_depth  # noqa: E402

# Boundary policy readers an MCP/host adapter needs to enforce limits and
# detect nested invocations. The `env` module itself stays internal; these
# specific readers are the supported public surface (DRY-101 keeps the
# parsing policy in env.py).
from .env import (  # noqa: E402
    max_fanout,
    max_background,
    root_identity,
)


# ---------- Caller (power-user entry point) ----------

class Caller:
    """Configurable runtime. Reuse across many `call()` invocations to share
    session discovery, channel config, and trace destination."""

    def __init__(
        self,
        *,
        session: Optional[str] = None,
        cwd: Optional[str] = None,
        model: Optional[str] = None,
        permission_mode: str = "default",
        on_permission: Optional[PermissionHandler] = None,
        max_depth: Optional[int] = None,
        timeout: int = 300,
        log_dir: Optional[Path] = None,
        invoke_id: Optional[str] = None,
        seed: Optional[int] = None,
    ):
        self._explicit_session = session
        self._cwd = cwd
        self._model = model
        self._permission_mode = permission_mode
        self._on_permission = on_permission or allow_all
        self._max_depth = max_depth if max_depth is not None else _default_max_depth()
        self._timeout = timeout
        # `log_dir` is the directory that holds `{invoke_id}/call_trace.jsonl`
        # and `{invoke_id}.yaml` reports. `invoke_id`, if provided, is reused
        # across every call on this Caller; otherwise a fresh id is generated
        # per invocation.
        self._log_dir = log_dir
        self._invoke_id = invoke_id
        self._seed = seed
        # Owns the nested-vs-root identity decision and child-env propagation.
        # Reads process env lazily (per call), so it stays correct even if a
        # host pops stale CALLSTACK_ROOT_* between Caller construction and use.
        self._inv = InvocationFactory(
            explicit_cwd=cwd,
            explicit_log_dir=log_dir,
            explicit_invoke_id=invoke_id,
            max_depth=self._max_depth,
        )

    def call(self, task: str, *, context: str = "fork") -> Result:
        results = self._invoke([task], context=context)
        return _unwrap_single(results[0])

    def call_many(self, tasks: Sequence[str], *,
                  context: str = "fork") -> MultiResult:
        return MultiResult(results=self._invoke(list(tasks), context=context))

    def close(self) -> None:
        """Tear down every pooled `claude` subprocess. The pool is a
        module-level singleton shared by all Callers, so calling close()
        from one Caller affects others — typically only called at
        program exit. Pooled processes are also drained by an
        `atexit` hook, so this is rarely needed explicitly."""
        shutdown_pool()

    def __enter__(self) -> "Caller":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def resume(self, token: YieldToken, reply: str) -> Result:
        clone = Path(token.clone_path)
        store = TreeStore()
        snapshot = store.load(clone)
        if snapshot is None:
            raise CallFailed(f"No saved tree at {clone}.call_tree — cannot resume")

        tree = Tree.from_dict(snapshot)
        parent = tree.root_session
        ctx = self._resolve_invocation_context(parent)
        driver = self._driver_for(parent, ctx=ctx)
        started_at = _utc_now_iso()
        report = InvocationReport.from_context(ctx)
        reporter = report.reporter(
            kind=ctx.prefix("call_resume"),
            tasks=[n.task for n in tree.nodes], started_at=started_at,
        )
        driver.on_progress = reporter
        # Same try/finally guarantee as `_invoke`: `tree` is already a valid
        # snapshot loaded above, so even a mid-resume exception leaves a
        # consistent on-disk report reflecting the partial progress.
        try:
            driver.resume(tree, token.session_id, reply)
        finally:
            # `report.seal` gives late `op:return`/`op:yield` envelopes a
            # chance to land on the child JSONL before finalizing — a node
            # that misses the window becomes `Timeout` rather than being
            # sealed as still-running. See PRD
            # `prd-don-t-seal-report-yaml-virtual-harp.md`.
            report.seal(reporter, tree)
        return _unwrap_single(_results_from_tree(tree)[0])

    # ---- internal ----

    def _invoke(self, tasks: list[str], *, context: str = "fork") -> list:
        """Run `tasks`, returning one entry per task: Result | CallYielded | CallFailed."""
        locator = SessionLocator()
        # In fresh+cross-project mode, `self._cwd` points at the child's
        # target folder, but the *parent* session lives in the caller's
        # project folder. Locate the parent session against the env-anchored
        # parent project, not the (possibly redirected) child cwd.
        parent_cwd = self._parent_project_cwd()
        parent = locator.locate(explicit=self._explicit_session, cwd=parent_cwd)
        depth = int(os.environ.get(ENV_DEPTH, "0"))
        ctx = self._resolve_invocation_context(parent)
        driver = self._driver_for(parent, ctx=ctx, depth_base=depth)
        started_at = _utc_now_iso()
        kind = ctx.prefix("call")
        report = InvocationReport.from_context(ctx)
        reporter = report.reporter(kind=kind, tasks=list(tasks),
                                   started_at=started_at)
        driver.on_progress = reporter
        # Try/finally ensures `report.yaml` is always finalized, even if
        # `driver.run` raises. Without this, a debounced merge could be
        # left scheduled and the on-disk report would be up to ~250ms
        # stale relative to the actual final state.
        tree: Optional[Tree] = None
        try:
            tree = driver.run(parent, tasks, base_depth=depth, context=context)
        finally:
            # If `driver.run` raised before assigning to `tree`, fall back
            # to `driver.last_tree` (stamped immediately after the Tree
            # object exists) so the reporter still gets a finalize pass.
            # Without this fallback the frame would be left with whatever
            # state the last `on_progress` tick recorded — typically
            # `awaiting_turn` — and `frames._reconcile_orphan_states`
            # would have to wait for the process to die before promoting
            # it to "abandoned". With the fallback, the frame is sealed
            # synchronously before the MCP boundary emits its tool_result.
            if tree is None:
                tree = driver.last_tree
            if tree is not None:
                # `report.seal` = wait-for-terminal-signals + reporter.finalize.
                report.seal(reporter, tree)
        # `tree is None` only reaches here when `driver.run` raised AND
        # never even stamped a partial tree onto itself — extremely rare
        # (would require failure inside `__init__`-level setup). The
        # `finally` above already ran; the exception is propagating in
        # that case so we never actually return.
        assert tree is not None
        return _results_from_tree(tree)

    def _parent_project_cwd(self) -> Optional[str]:
        """Project folder of the caller's session (delegated to the factory) —
        needed for cross-project fresh calls where `self._cwd` is the child's
        target, not the parent's project."""
        return self._inv.parent_project_cwd()

    def _resolve_invocation_context(self, parent: SessionRef) -> _InvocationContext:
        """Decide whether this call is root or nested (delegated to the
        factory). Nested calls inherit the root's `invoke_id` + `log_dir` so
        their tree merges under the caller's node in the root's report."""
        return self._inv.context(parent.cwd)

    def _driver_for(self, parent: SessionRef, *, ctx: _InvocationContext,
                    depth_base: int = 0) -> Driver:
        # Identity + child-env propagation live in the factory; the channel /
        # trace / store wiring is this Caller's runtime config.
        channel = ClaudeChannel(
            model=self._model,
            permission_mode=self._permission_mode,
            permission_handler=self._on_permission,
            env=self._inv.child_env(ctx, depth_base=depth_base),
        )
        return Driver(
            channel=channel,
            resolve_session=SessionLocator().resolve,
            trace=TraceWriter(ctx.invocation_dir),
            store=TreeStore(),
            cwd=self._inv.effective_cwd(parent.cwd),
            timeout=self._timeout,
            max_depth=self._max_depth,
            seed=self._seed,
        )


# ---------- Module-level convenience wrappers ----------

_default: Optional[Caller] = None


def _shared() -> Caller:
    global _default
    if _default is None:
        _default = Caller()
    return _default


def _resolve_caller(seed: Optional[int], timeout: Optional[int]) -> Caller:
    """ARCH-12: shared resolution between module-level wrappers.

    Returns a fresh `Caller` when seed or timeout is overridden, else the
    shared singleton."""
    if seed is None and timeout is None:
        return _shared()
    return Caller(timeout=timeout or 300, seed=seed)


def call(task: str, *, seed: Optional[int] = None,
         timeout: Optional[int] = None,
         context: str = "fork") -> Result:
    """Fork a child agent on `task`. Returns the child's `Result`. Raises
    `CallYielded` if the agent paused for input, `CallFailed` on error.

    context: "fork" (default) — child inherits the parent's transcript.
             "fresh"           — child starts a brand-new session with no
                                 inherited context (Claude Code Agent-tool
                                 semantics).

    seed: opaque integer recorded into traces for downstream trial grouping
        (e.g., pass^k disaggregation). Does NOT produce deterministic provider
        output — the Anthropic API has no seed parameter as of 2026-04. Use
        this to label trials, not to expect bitwise reproducibility.
    """
    return _resolve_caller(seed, timeout).call(task, context=context)


def call_many(tasks: Sequence[str], *, seed: Optional[int] = None,
              timeout: Optional[int] = None,
              context: str = "fork") -> MultiResult:
    return _resolve_caller(seed, timeout).call_many(tasks, context=context)


def resume(token: YieldToken, reply: str, *, seed: Optional[int] = None,
           timeout: Optional[int] = None) -> Result:
    return _resolve_caller(seed, timeout).resume(token, reply)


# ARCH-13: the legacy one-shot writer `_write_invocation_report` previously
# lived here for tests only. It has moved to tests/_helpers.py
# (`write_invocation_report`). Production code uses `_LiveReporter`.
