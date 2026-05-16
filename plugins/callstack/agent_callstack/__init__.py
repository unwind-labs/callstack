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
import uuid
from pathlib import Path
from typing import Optional, Sequence

from .channel import ClaudeChannel, PermissionHandler, allow_all, shutdown_pool
from .driver import Driver, Node, Tree
from .frames import (
    _ROOT_FRAME_KEY,
    _build_merged_report,
    _frames_cache_clear,
    _load_frames,
    _most_recent_session,
)
from .invocation_ctx import _InvocationContext, _new_invoke_id, _utc_now_iso
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
from .trace import TraceWriter, TreeStore


__all__ = [
    "call", "call_many", "resume",
    "Caller", "Result", "YieldToken",
    "CallYielded", "CallFailed", "MultiResult",
]


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
)
from .env import max_depth as _default_max_depth  # noqa: E402


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
        reporter = _LiveReporter(
            ctx=ctx, kind=ctx.prefix("call_resume"),
            tasks=[n.task for n in tree.nodes], started_at=started_at,
        )
        driver.on_progress = reporter
        # Same try/finally guarantee as `_invoke`: `tree` is already a valid
        # snapshot loaded above, so even a mid-resume exception leaves a
        # consistent on-disk report reflecting the partial progress.
        try:
            driver.resume(tree, token.session_id, reply)
        finally:
            reporter.finalize(tree)
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
        reporter = _LiveReporter(ctx=ctx, kind=kind, tasks=list(tasks),
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
            if tree is not None:
                reporter.finalize(tree)
        # `tree is None` only when `driver.run` raised — the finally above
        # already ran, and the exception is propagating; we never reach here
        # in that case. The assert is for type narrowing.
        assert tree is not None
        return _results_from_tree(tree)

    def _parent_project_cwd(self) -> Optional[str]:
        """Project folder of the caller's session — needed for cross-project
        fresh calls where `self._cwd` is the child's target, not the parent's
        project. Falls back through env, explicit cwd, then os.getcwd()."""
        # We're inside a nested invocation iff the parent stamped its root
        # identity into our env. ENV_ROOT_INVOKE_ID is the canonical
        # "we're nested" signal; the legacy ENV_PARENT_SESSION env var
        # was removed (its inherited value caused the regression where
        # grandchildren forked from root instead of their immediate parent).
        if os.environ.get(ENV_ROOT_INVOKE_ID):
            try:
                # MCP server's os.getcwd() is reliably the caller's project
                # folder; trust it over self._cwd (which may be a redirected
                # child target in cross-project fresh calls).
                return os.getcwd()
            except OSError:
                pass
        return self._cwd or os.getcwd()

    def _effective_cwd(self, parent: SessionRef) -> str:
        return self._cwd or parent.cwd or os.getcwd()

    def _effective_log_dir(self, cwd: str) -> Path:
        return self._log_dir or (Path(cwd) / ".claude" / "callstack" / "log")

    def _resolve_invocation_context(self, parent: SessionRef) -> _InvocationContext:
        """Decide whether this call is a top-level (root) invocation or nested
        inside an already-running one. Nested calls inherit the root's
        `invoke_id` + `log_dir` from env so their tree can be merged under
        the caller's node in the root's report."""
        effective_cwd = self._effective_cwd(parent)
        root_id_env = os.environ.get(ENV_ROOT_INVOKE_ID)
        root_log_env = os.environ.get(ENV_ROOT_LOG_DIR)
        if root_id_env and root_log_env:
            # Deterministic: the parent Driver stamped this subprocess with
            # the caller node's id via CALLSTACK_FRAME_KEY. Fall back to
            # session heuristics only if the env didn't survive (shouldn't
            # happen with a current agent-callstack parent, but keeps us
            # robust when nested under an older runtime).
            frame_key = (
                os.environ.get(ENV_FRAME_KEY)
                or os.environ.get(ENV_CLAUDE_SESSION)
                or _most_recent_session(effective_cwd)
                or f"pid-{os.getpid()}"
            )
            return _InvocationContext(
                invoke_id=root_id_env,
                log_dir=Path(root_log_env),
                cwd=effective_cwd,
                frame_key=frame_key,
                is_nested=True,
                # Unique per nested invocation so multiple sibling invokes
                # from the same caller (e.g. a deep-rewrite fork running
                # specialists, then meta-assessors, then re-author) don't
                # share — and overwrite — one frame file.
                instance_id=uuid.uuid4().hex[:12],
            )
        return _InvocationContext(
            invoke_id=self._invoke_id or _new_invoke_id(),
            log_dir=self._effective_log_dir(effective_cwd),
            cwd=effective_cwd,
            frame_key=_ROOT_FRAME_KEY,
            is_nested=False,
        )

    def _driver_for(self, parent: SessionRef, *, ctx: _InvocationContext,
                    depth_base: int = 0) -> Driver:
        cwd = self._effective_cwd(parent)
        # Children inherit the depth via env so nested CALLs respect max_depth.
        # Root identity propagates so nested MCP invokes can find and merge
        # into this same invocation's report.
        #
        # We deliberately do NOT propagate CALLSTACK_PARENT_SESSION (legacy
        # env removed) — its inherited value caused grandchildren to
        # resolve the *root* as their parent. The child's own session
        # UUID is instead stamped by `ClaudeChannel._spawn()` as
        # CALLSTACK_OWN_SESSION, paired with `--session-id <uuid>` on
        # the spawned claude's argv, so it's deterministic and immune
        # to env inheritance across spawn depth.
        env = {
            ENV_DEPTH: str(depth_base + 1),
            ENV_ROOT_INVOKE_ID: ctx.invoke_id,
            ENV_ROOT_LOG_DIR: str(ctx.log_dir),
            # CORR-101: stamp the effective max_depth onto every spawned
            # child so a grandchild doesn't silently revert to the default
            # cap when the root explicitly chose a smaller one. Without
            # this, ENV_MAX_DEPTH is only honored if the caller (or a
            # human's shell) happened to export it — a per-Caller
            # max_depth=3 would be ignored by claude subprocesses that
            # inherit only the unset env, and ENV_DEPTH alone would
            # compare against the child's default 10.
            ENV_MAX_DEPTH: str(self._max_depth),
        }
        channel = ClaudeChannel(
            model=self._model,
            permission_mode=self._permission_mode,
            permission_handler=self._on_permission,
            env=env,
        )
        return Driver(
            channel=channel,
            resolve_session=SessionLocator().resolve,
            trace=TraceWriter(ctx.invocation_dir),
            store=TreeStore(),
            cwd=cwd,
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
