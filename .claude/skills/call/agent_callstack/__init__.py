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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .channel import ClaudeChannel, PermissionHandler, allow_all
from .driver import Driver, Node, Tree
from .session import SessionLocator, SessionRef
from .trace import TraceWriter, TreeStore


__all__ = [
    "call", "call_many", "resume",
    "Caller", "Result", "YieldToken",
    "CallYielded", "CallFailed", "MultiResult",
]


# ---------- Public value types ----------

@dataclass(frozen=True)
class YieldToken:
    """Opaque handle for resuming a yielded session. Pass to `resume()`."""
    session_id: str
    clone_path: str


@dataclass(frozen=True)
class Result:
    value: Any
    summary: Optional[str]
    next: Optional[str]
    duration: float
    log: Optional[Path]
    log_start: int


@dataclass(frozen=True)
class MultiResult:
    """Returned by `call_many` (mixed completes/errors/yields)."""
    results: list  # list[Result | CallFailed | CallYielded]


class CallYielded(Exception):
    """Raised when an agent emits YIELD. Carries the resume token + question."""
    def __init__(self, question: str, token: YieldToken):
        super().__init__(question)
        self.question = question
        self.token = token


class CallFailed(Exception):
    """Raised when an agent or its descendants fail. Carries any partial output."""
    def __init__(self, error: str, partial: Any = None):
        super().__init__(error)
        self.error = error
        self.partial = partial


# ---------- Caller (power-user entry point) ----------

ENV_DEPTH = "CALLSTACK_DEPTH"
ENV_PARENT_SESSION = "CALLSTACK_PARENT_SESSION"


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
        max_depth: int = 5,
        timeout: int = 300,
        trace_dir: Optional[Path] = None,
        seed: Optional[int] = None,
    ):
        self._explicit_session = session
        self._cwd = cwd
        self._model = model
        self._permission_mode = permission_mode
        self._on_permission = on_permission or allow_all
        self._max_depth = max_depth
        self._timeout = timeout
        self._trace_dir = trace_dir
        self._seed = seed

    def call(self, task: str) -> Result:
        results = self._invoke([task])
        return _unwrap_single(results[0])

    def call_many(self, tasks: Sequence[str]) -> MultiResult:
        wrapped = [_wrap(item) for item in self._invoke(list(tasks))]
        return MultiResult(results=wrapped)

    def resume(self, token: YieldToken, reply: str) -> Result:
        clone = Path(token.clone_path)
        store = TreeStore()
        snapshot = store.load(clone)
        if snapshot is None:
            raise CallFailed(f"No saved tree at {clone}.call_tree — cannot resume")

        tree = Tree.from_dict(snapshot)
        parent = tree.root_session
        driver = self._driver_for(parent)
        driver.resume(tree, token.session_id, reply)
        return _unwrap_single(_result_from_tree(tree))

    # ---- internal ----

    def _invoke(self, tasks: list[str]) -> list:
        """Run `tasks`, returning one entry per task: Result | CallYielded | CallFailed."""
        locator = SessionLocator()
        parent = locator.locate(explicit=self._explicit_session, cwd=self._cwd)
        depth = int(os.environ.get(ENV_DEPTH, "0"))
        driver = self._driver_for(parent, depth_base=depth)
        tree = driver.run(parent, tasks, base_depth=depth)
        return _results_from_tree(tree)

    def _driver_for(self, parent: SessionRef, depth_base: int = 0) -> Driver:
        cwd = self._cwd or parent.cwd or os.getcwd()
        # Children inherit the depth via env so nested CALLs respect max_depth.
        env = {
            ENV_DEPTH: str(depth_base + 1),
            ENV_PARENT_SESSION: str(parent.file),
        }
        channel = ClaudeChannel(
            model=self._model,
            permission_mode=self._permission_mode,
            permission_handler=self._on_permission,
            env=env,
        )
        trace_dir = self._trace_dir or (parent.file.parent / "call_traces")
        return Driver(
            channel=channel,
            locator=SessionLocator(),
            trace=TraceWriter(trace_dir),
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


def call(task: str, *, seed: Optional[int] = None,
         timeout: Optional[int] = None) -> Result:
    """Fork a child agent on `task`. Returns the child's `Result`. Raises
    `CallYielded` if the agent paused for input, `CallFailed` on error.

    seed: opaque integer recorded into traces for downstream trial grouping
        (e.g., pass^k disaggregation). Does NOT produce deterministic provider
        output — the Anthropic API has no seed parameter as of 2026-04. Use
        this to label trials, not to expect bitwise reproducibility.
    """
    if timeout is not None or seed is not None:
        return Caller(timeout=timeout or 300, seed=seed).call(task)
    return _shared().call(task)


def call_many(tasks: Sequence[str], *, seed: Optional[int] = None,
              timeout: Optional[int] = None) -> MultiResult:
    if timeout is not None or seed is not None:
        return Caller(timeout=timeout or 300, seed=seed).call_many(tasks)
    return _shared().call_many(tasks)


def resume(token: YieldToken, reply: str, *, seed: Optional[int] = None,
           timeout: Optional[int] = None) -> Result:
    if timeout is not None or seed is not None:
        return Caller(timeout=timeout or 300, seed=seed).resume(token, reply)
    return _shared().resume(token, reply)


# ---------- Tree → public result translation ----------

def _result_from_node(node: Node):
    """Convert a finished node into Result / CallYielded / CallFailed."""
    s = node.state
    if s.kind == "done":
        return Result(
            value=node.result, summary=node.summary, next=node.suggested_next,
            duration=round(node.duration, 2),
            log=Path(node.clone_path) if node.clone_path else None,
            log_start=node.parent_lines + 1,
        )
    if s.kind == "failed":
        return CallFailed(error=node.error or "unknown error", partial=node.result)
    if s.kind == "awaiting_user":
        # Wraps the leaf the user must answer.
        return CallYielded(
            question=node.state.question,  # type: ignore[union-attr]
            token=YieldToken(session_id=node.session_id or "",
                             clone_path=node.clone_path or ""),
        )
    # Should not happen — drive() returned with an in-flight node.
    return CallFailed(error=f"node ended in unexpected state: {s.kind}")


def _results_from_tree(tree: Tree) -> list:
    out = []
    for root in tree.nodes:
        leaf = root.yielded_descendant()
        if leaf is not None:
            out.append(_result_from_node(leaf))
        else:
            out.append(_result_from_node(root))
    return out


def _result_from_tree(tree: Tree) -> list:
    return _results_from_tree(tree)


def _unwrap_single(item) -> Result:
    if isinstance(item, Result):
        return item
    if isinstance(item, (CallYielded, CallFailed)):
        raise item
    raise CallFailed(error=f"unexpected result type: {type(item).__name__}")


def _wrap(item):
    """Identity for serialization; kept as a hook for future shaping."""
    return item
