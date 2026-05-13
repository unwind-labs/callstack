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
    _wrap,
)
from .session import PROJECTS_DIR, SessionLocator, SessionRef, encode_project_dir
from .trace import TraceWriter, TreeStore


__all__ = [
    "call", "call_many", "resume",
    "Caller", "Result", "YieldToken",
    "CallYielded", "CallFailed", "MultiResult",
]


# ---------- Env constants ----------

ENV_DEPTH = "CALLSTACK_DEPTH"
ENV_PARENT_SESSION = "CALLSTACK_PARENT_SESSION"
# Propagate the root invocation identity into every spawned claude subprocess,
# so a nested MCP `invoke`/`invoke_parallel` call can merge its tree into the
# root's report instead of starting a fresh top-level invocation.
ENV_ROOT_INVOKE_ID = "CALLSTACK_ROOT_INVOKE_ID"
ENV_ROOT_LOG_DIR = "CALLSTACK_ROOT_LOG_DIR"
# The parent Driver stamps each forked subprocess with this env — equal to
# the spawned node's id. A nested MCP invoke inside that subprocess reads
# it back to identify its frame deterministically (no session-id guessing).
ENV_FRAME_KEY = "CALLSTACK_FRAME_KEY"
# Set by Claude CLI inside a forked session; identifies the caller node.
# Used only as a fallback when `CALLSTACK_FRAME_KEY` is absent.
ENV_CLAUDE_SESSION = "CLAUDE_SESSION_ID"
# Optional override for the default max_depth — picked up at Caller
# construction time. Inherited by forked subprocesses unchanged.
ENV_MAX_DEPTH = "CALLSTACK_MAX_DEPTH"


def _default_max_depth() -> int:
    raw = os.environ.get(ENV_MAX_DEPTH)
    if raw is None:
        return 10
    try:
        v = int(raw)
        return v if v > 0 else 10
    except ValueError:
        return 10


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
        wrapped = [_wrap(item) for item in self._invoke(list(tasks), context=context)]
        return MultiResult(results=wrapped)

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
        driver.resume(tree, token.session_id, reply)
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
        tree = driver.run(parent, tasks, base_depth=depth, context=context)
        reporter.finalize(tree)
        return _results_from_tree(tree)

    def _parent_project_cwd(self) -> Optional[str]:
        """Project folder of the caller's session — needed for cross-project
        fresh calls where `self._cwd` is the child's target, not the parent's
        project. Falls back through env, explicit cwd, then os.getcwd()."""
        env_parent = os.environ.get(ENV_PARENT_SESSION)
        if env_parent:
            try:
                # ENV_PARENT_SESSION holds the parent session's JSONL path.
                # Its parent dir is `~/.claude/projects/<encoded-cwd>/`,
                # which we don't need to decode — SessionLocator can use any
                # cwd to find the session; what matters is that we pick a
                # cwd that resolves to the *caller's* project, not the
                # redirected child cwd. The MCP server's actual os.getcwd()
                # is reliably the caller's project folder.
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
        env = {
            ENV_DEPTH: str(depth_base + 1),
            ENV_PARENT_SESSION: str(parent.file),
            ENV_ROOT_INVOKE_ID: ctx.invoke_id,
            ENV_ROOT_LOG_DIR: str(ctx.log_dir),
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
