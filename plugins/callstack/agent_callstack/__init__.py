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

import contextlib
import datetime as dt
import fcntl
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

import yaml

from .channel import ClaudeChannel, PermissionHandler, allow_all
from .driver import Driver, Node, Tree
from .session import PROJECTS_DIR, SessionLocator, SessionRef, encode_project_dir
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
        log_dir: Optional[Path] = None,
        invoke_id: Optional[str] = None,
        seed: Optional[int] = None,
    ):
        self._explicit_session = session
        self._cwd = cwd
        self._model = model
        self._permission_mode = permission_mode
        self._on_permission = on_permission or allow_all
        self._max_depth = max_depth
        self._timeout = timeout
        # `log_dir` is the directory that holds `{invoke_id}/call_trace.jsonl`
        # and `{invoke_id}.yaml` reports. `invoke_id`, if provided, is reused
        # across every call on this Caller; otherwise a fresh id is generated
        # per invocation.
        self._log_dir = log_dir
        self._invoke_id = invoke_id
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

    def _invoke(self, tasks: list[str]) -> list:
        """Run `tasks`, returning one entry per task: Result | CallYielded | CallFailed."""
        locator = SessionLocator()
        parent = locator.locate(explicit=self._explicit_session, cwd=self._cwd)
        depth = int(os.environ.get(ENV_DEPTH, "0"))
        ctx = self._resolve_invocation_context(parent)
        driver = self._driver_for(parent, ctx=ctx, depth_base=depth)
        started_at = _utc_now_iso()
        kind = ctx.prefix("call")
        reporter = _LiveReporter(ctx=ctx, kind=kind, tasks=list(tasks),
                                 started_at=started_at)
        driver.on_progress = reporter
        tree = driver.run(parent, tasks, base_depth=depth)
        reporter.finalize(tree)
        return _results_from_tree(tree)

    def _effective_cwd(self, parent: SessionRef) -> str:
        return self._cwd or parent.cwd or os.getcwd()

    def _effective_log_dir(self, cwd: str) -> Path:
        return self._log_dir or (Path(cwd) / ".claude" / "callstack" / "log")

    def _resolve_invocation_context(self, parent: SessionRef) -> "_InvocationContext":
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

    def _driver_for(self, parent: SessionRef, *, ctx: "_InvocationContext",
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
            locator=SessionLocator(),
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


def _unwrap_single(item) -> Result:
    if isinstance(item, Result):
        return item
    if isinstance(item, (CallYielded, CallFailed)):
        raise item
    raise CallFailed(error=f"unexpected result type: {type(item).__name__}")


def _wrap(item):
    """Identity for serialization; kept as a hook for future shaping."""
    return item


# ---------- Invocation-report writer ----------

def _new_invoke_id() -> str:
    """Sortable, collision-resistant id: `YYYYMMDDTHHMMSS-<8 hex>`."""
    return f"{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


_ROOT_FRAME_KEY = "root"


@dataclass(frozen=True)
class _InvocationContext:
    """Where a Caller writes its per-invocation artifacts.

    ``instance_id`` disambiguates the frame *file* when multiple nested
    invocations share the same ``frame_key`` (i.e. the same caller node
    issues several sibling ``invoke*`` calls). Empty string preserves the
    legacy ``{frame_key}.yaml`` filename — used for the root frame and for
    tests that construct the context directly without setting it.

    For the root (top-level) invocation `frame_key == "root"` and the Caller
    owns the full invocation directory. For a nested MCP call — detected via
    `CALLSTACK_ROOT_*` env — the Caller reuses the root's invocation directory
    and writes its own tree to `_frames/{caller_session}.yaml`, where the
    root's `_LiveReporter` picks it up and grafts it under the caller's node."""

    invoke_id: str
    log_dir: Path
    cwd: str
    frame_key: str
    is_nested: bool
    instance_id: str = ""

    @property
    def invocation_dir(self) -> Path:
        return self.log_dir / self.invoke_id

    @property
    def frames_dir(self) -> Path:
        return self.invocation_dir / "_frames"

    @property
    def report_path(self) -> Path:
        return self.invocation_dir / "report.yaml"

    @property
    def log_path(self) -> Path:
        return self.invocation_dir / "progress.log"

    @property
    def lock_path(self) -> Path:
        return self.invocation_dir / ".report.lock"

    def frame_path(self, key: Optional[str] = None) -> Path:
        # Explicit key override (used by callers that need to read a peer
        # frame) — keep the legacy single-file filename.
        if key is not None:
            return self.frames_dir / f"{key}.yaml"
        # Production nested invocations carry a unique ``instance_id`` so
        # multiple sibling invokes from the same caller don't overwrite
        # each other's frame. The frame's ``frame_key`` field still pins it
        # to the caller node for grafting.
        if self.instance_id:
            return self.frames_dir / f"{self.frame_key}-{self.instance_id}.yaml"
        return self.frames_dir / f"{self.frame_key}.yaml"

    def prefix(self, kind: str) -> str:
        return f"nested_{kind}" if self.is_nested else kind


class _LiveReporter:
    """`Driver.on_progress` callback — rewrites the merged report.yaml and
    appends per-transition lines to a shared tail-friendly log.

    Each invocation writes its own frame (`_frames/{key}.yaml`) containing
    its Tree. On every update the reporter scans all frames, grafts each
    non-root frame's nodes under the root node whose session_id matches
    the frame key, and writes the combined report. A cross-process
    `fcntl.flock` serializes the merge so parent and nested writers can't
    corrupt each other's updates; an in-process lock serializes parallel
    roots in the same `call_many`."""

    def __init__(self, *, ctx: _InvocationContext, kind: str,
                 tasks: Sequence[str], started_at: str):
        self._ctx = ctx
        self._kind = kind
        self._tasks = list(tasks)
        self._started_at = started_at
        self._prev_status: dict[str, str] = {}
        self._thread_lock = threading.Lock()

    def __call__(self, tree: Tree) -> None:
        self._write(tree, ended_at=_utc_now_iso())

    def finalize(self, tree: Tree) -> None:
        """Last write after the driver returns, so `ended_at` reflects real end."""
        self._write(tree, ended_at=_utc_now_iso())
        # Root's finalize runs strictly after every nested finalize (parent
        # driver.run() doesn't return until all forks complete), so it's
        # safe to remove the lock file here — nothing else will need it.
        if not self._ctx.is_nested:
            try:
                self._ctx.lock_path.unlink()
            except FileNotFoundError:
                pass

    def _write(self, tree: Tree, *, ended_at: str) -> None:
        with self._thread_lock:
            self._ctx.invocation_dir.mkdir(parents=True, exist_ok=True)
            self._ctx.frames_dir.mkdir(parents=True, exist_ok=True)
            self._write_frame(tree, ended_at=ended_at)
            with _interprocess_lock(self._ctx.lock_path):
                self._rewrite_merged_report(ended_at=ended_at)
            self._append_transitions(tree, ended_at)

    # ---- per-frame snapshot ----

    def _write_frame(self, tree: Tree, *, ended_at: str) -> None:
        frame = {
            "frame_key": self._ctx.frame_key,
            "is_nested": self._ctx.is_nested,
            "kind": self._kind,
            "tasks": self._tasks,
            "cwd": self._ctx.cwd,
            "started_at": self._started_at,
            "ended_at": ended_at,
            "tree": tree.to_dict(),
        }
        _atomic_yaml_write(self._ctx.frame_path(), frame)

    # ---- merged report ----

    def _rewrite_merged_report(self, *, ended_at: str) -> None:
        frames = _load_frames(self._ctx.frames_dir)
        root_frames = frames.get(_ROOT_FRAME_KEY)
        if not root_frames:
            # Nested wrote first and root hasn't written yet — skip; the
            # root's next progress tick will rewrite the merged report.
            return
        doc = _build_merged_report(
            invoke_id=self._ctx.invoke_id, frames=frames,
            root_frame=root_frames[0], ended_at=ended_at,
        )
        _atomic_yaml_write(self._ctx.report_path, doc)

    # ---- shared append-only log ----

    def _append_transitions(self, tree: Tree, ts: str) -> None:
        # For nested, prefix every line's id-chain with the caller's node
        # id (looked up from root.yaml by matching session_id). Cached so
        # we don't re-read the root frame on every line.
        ancestor_chain = self._ancestor_chain()
        lines: list[str] = []
        for node, depth, chain in _walk_tree(tree, ancestor_chain):
            if self._prev_status.get(node.id) == node.status:
                continue
            lines.append(_format_log_line(ts, node, depth, chain=chain))
            self._prev_status[node.id] = node.status
        if not lines:
            return
        # O_APPEND on POSIX makes line-sized writes atomic; safe across
        # processes without a lock.
        with open(self._ctx.log_path, "a") as f:
            f.write("\n".join(lines) + "\n")

    def _ancestor_chain(self) -> list[str]:
        """Short node ids from root down to the node that spawned this
        invocation. Empty for root; for a deeply-nested call it's e.g.
        ['c_short', 'e_short'] so G's lines read `[c→e→g]`.

        Walks the *merged* tree built from every frame file — so a level-3
        call (G nested under E nested under C) sees E's node in the
        nested-C frame, and recursively that frame's own caller chain."""
        if not self._ctx.is_nested:
            return []
        cached = getattr(self, "_cached_ancestor_chain", None)
        if cached is not None:
            return cached
        frames = _load_frames(self._ctx.frames_dir)
        if _ROOT_FRAME_KEY not in frames:
            return []  # root hasn't landed yet; will resolve on next tick
        merged = _merge_raw_nodes(frames)
        chain = _chain_to_session(merged, self._ctx.frame_key) or []
        if chain:
            self._cached_ancestor_chain = chain
        return chain


# ---------- cross-process lock ----------

@contextlib.contextmanager
def _interprocess_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _atomic_yaml_write(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False,
                       width=120, allow_unicode=True)
    os.replace(tmp, path)


# ---------- frame loading + merging ----------

def _load_frames(frames_dir: Path) -> dict[str, list[dict]]:
    """Load every frame file under ``frames_dir``, grouped by ``frame_key``.

    Returns a list per key because a caller node may have issued several
    sibling nested invocations that all share its frame_key but live in
    distinct files (disambiguated by ``instance_id`` in the filename).
    Frames in each list are sorted by ``started_at`` so grafting is stable.
    """
    out: dict[str, list[dict]] = {}
    if not frames_dir.is_dir():
        return out
    for p in frames_dir.glob("*.yaml"):
        try:
            d = yaml.safe_load(p.read_text())
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        key = d.get("frame_key")
        if isinstance(key, str):
            out.setdefault(key, []).append(d)
    for key in out:
        out[key].sort(key=lambda f: str(f.get("started_at") or ""))
    return out


def _build_merged_report(*, invoke_id: str, frames: dict[str, list[dict]],
                         root_frame: dict, ended_at: str) -> dict:
    """Produce the report.yaml document by grafting each non-root frame's
    tree under the node (anywhere in the root's tree) whose session_id
    matches the frame key. Multiple frames may share a key (one per
    sibling nested invocation); their nodes are concatenated under the
    matching caller node, in started_at order."""
    root_tree = root_frame.get("tree", {})
    root_nodes = root_tree.get("nodes", []) or []
    nested_by_session = {k: v for k, v in frames.items() if k != _ROOT_FRAME_KEY}
    tasks = root_frame.get("tasks") or []
    merged_nodes = [
        _graft_node(n, tasks[i] if i < len(tasks) else n.get("task", ""),
                    depth=root_tree.get("base_depth", 0) + 1,
                    nested_by_session=nested_by_session)
        for i, n in enumerate(root_nodes)
    ]
    overall = _status_of_nodes(merged_nodes)
    return {
        "invoke_id": invoke_id,
        "kind": root_frame.get("kind"),
        "cwd": root_frame.get("cwd"),
        "parent_session": root_tree.get("root_session_id"),
        "base_depth": root_tree.get("base_depth", 0),
        "started_at": root_frame.get("started_at"),
        "ended_at": ended_at,
        "duration_seconds": round(
            sum(float(n.get("duration_seconds", 0.0)) for n in merged_nodes), 2,
        ),
        "status": overall,
        "nested_frames": sorted(nested_by_session.keys()),
        "tasks": merged_nodes,
    }


def _graft_node(node_dict: dict, input_text: str, *, depth: int,
                nested_by_session: dict[str, list[dict]]) -> dict:
    """Render one Node.to_dict() into report shape, attaching nested-frame
    children whose frame key matches this node's id (preferred, set by the
    parent Driver via CALLSTACK_FRAME_KEY) or session_id (fallback). When
    multiple frames share that key (sibling nested invocations from the
    same caller), all of their nodes graft in — sorted by frame
    ``started_at`` so order is stable."""
    sid = node_dict.get("session_id")
    nid = str(node_dict.get("id", ""))
    children_raw = list(node_dict.get("children") or [])
    matched_frames = nested_by_session.get(nid) or (
        nested_by_session.get(sid) if sid else None
    ) or []
    for mf in matched_frames:
        nested_nodes = (mf.get("tree") or {}).get("nodes") or []
        children_raw.extend(nested_nodes)
    children = [
        _graft_node(c, c.get("task", ""), depth=depth + 1,
                    nested_by_session=nested_by_session)
        for c in children_raw
    ]
    out: dict = {
        "id": str(node_dict.get("id", ""))[:8],
        "task": node_dict.get("task"),
        "status": _status_label_from_state(node_dict.get("state")),
        "depth": depth,
        "session_id": sid,
        "clone_path": node_dict.get("clone_path"),
        "duration_seconds": round(float(node_dict.get("duration", 0.0)), 2),
        "max_context_tokens_seen": node_dict.get("max_context_tokens_seen"),
        "input": input_text,
        "output": node_dict.get("result"),
        "summary": node_dict.get("summary"),
        "suggested_next": node_dict.get("suggested_next"),
        "error": node_dict.get("error"),
    }
    if children:
        out["children"] = children
    return out


_STATUS_FROM_STATE = {
    "pending": "pending",
    "awaiting_turn": "running",
    "awaiting_child": "running",
    "awaiting_user": "yielded",
    "done": "complete",
    "failed": "error",
}


def _status_label_from_state(state: Any) -> str:
    if isinstance(state, dict):
        return _STATUS_FROM_STATE.get(state.get("kind", ""), "unknown")
    return "unknown"


def _status_of_nodes(nodes: list[dict]) -> str:
    statuses = {n.get("status") for n in nodes}
    if not statuses:
        return "empty"
    if statuses == {"complete"}:
        return "complete"
    if "yielded" in statuses:
        return "yielded"
    if statuses == {"error"}:
        return "error"
    return "mixed"


def _walk_tree(tree: Tree, ancestor_chain: Optional[list[str]] = None):
    """Yield `(node, depth, chain)` where `chain` is the list of short node
    ids from the outermost ancestor down to (but not including) this node."""
    chain: list[str] = list(ancestor_chain or [])

    def walk(node: Node, d: int, current_chain: list[str]):
        yield node, d, current_chain
        next_chain = current_chain + [node.id[:8]]
        for c in node.children:
            yield from walk(c, d + 1, next_chain)
    for root in tree.nodes:
        yield from walk(root, tree.base_depth + 1, chain)


def _format_log_line(ts: str, node: Node, depth: int, *, chain: list[str]) -> str:
    indent = "  " * (depth - 1)
    short_id = node.id[:8]
    # Full chain up to and including this node, arrow-joined. Makes it
    # trivial to see "which ancestor spawned this" in tail output.
    id_chain = "→".join(chain + [short_id])
    task = _one_line(node.task, 60)
    detail = ""
    if node.status == "complete" and node.result is not None:
        detail = f'  result="{_one_line(str(node.result), 60)}"'
    elif node.status == "error" and node.error:
        detail = f'  error="{_one_line(node.error, 60)}"'
    elif node.status == "yielded":
        detail = "  (awaiting user)"
    return (f"[{ts}] d={depth} {indent}[{id_chain}] "
            f"{node.status:<9} task=\"{task}\"{detail}")


def _merge_raw_nodes(frames: dict[str, list[dict]]) -> list[dict]:
    """Build the full merged tree in raw `Node.to_dict()` shape (full ids
    preserved), recursively grafting every nested frame under the node
    whose id or session matches the frame's key. Used for chain lookups
    that need to reach nodes living inside nested-frame sidecars."""
    root_frames = frames.get(_ROOT_FRAME_KEY)
    if not root_frames:
        return []
    nested = {k: v for k, v in frames.items() if k != _ROOT_FRAME_KEY}
    root_nodes = (root_frames[0].get("tree") or {}).get("nodes") or []
    return [_graft_raw(n, nested) for n in root_nodes]


def _graft_raw(node: dict, nested: dict[str, list[dict]]) -> dict:
    nid = str(node.get("id", ""))
    sid = node.get("session_id")
    children = list(node.get("children") or [])
    matched = nested.get(nid) or (nested.get(sid) if sid else None) or []
    for frame in matched:
        frame_nodes = (frame.get("tree") or {}).get("nodes") or []
        children.extend(frame_nodes)
    return {**node, "children": [_graft_raw(c, nested) for c in children]}


def _chain_to_session(nodes: list, target: str) -> Optional[list[str]]:
    """DFS the root frame's nodes for one matching `target` (either a full
    node id or a session id). Return the short-id chain ending at that
    node (inclusive), or None if not found."""
    def walk(node_list: list, path: list[str]) -> Optional[list[str]]:
        for n in node_list:
            if not isinstance(n, dict):
                continue
            full_id = str(n.get("id", ""))
            short_id = full_id[:8]
            sid = n.get("session_id")
            new_path = path + [short_id]
            if full_id == target or sid == target:
                return new_path
            hit = walk(n.get("children") or [], new_path)
            if hit is not None:
                return hit
        return None
    return walk(nodes, [])


def _most_recent_session(cwd: str) -> Optional[str]:
    """Stem of the most recently modified `.jsonl` in the cwd's project dir.

    Used to identify the calling claude session when CLAUDE_SESSION_ID is
    not exported. The active fork is the one currently being appended to,
    so it wins by mtime."""
    proj_dir = PROJECTS_DIR / encode_project_dir(cwd)
    if not proj_dir.is_dir():
        return None
    best: Optional[str] = None
    best_mtime: float = 0.0
    for f in proj_dir.glob("*.jsonl"):
        try:
            m = f.stat().st_mtime
        except OSError:
            continue
        if m > best_mtime:
            best_mtime, best = m, f.stem
    return best


def _one_line(s: str, limit: int) -> str:
    s = s.replace("\n", " ").replace("\r", " ").replace('"', "'")
    return s if len(s) <= limit else s[: limit - 1] + "…"


# ---------- legacy one-shot writer (tests / external callers) ----------

def _write_invocation_report(
    *,
    log_dir: Path,
    invoke_id: str,
    kind: str,
    tasks: Sequence[str],
    tree: Tree,
    cwd: str,
    started_at: str,
    ended_at: str,
) -> Path:
    """One-shot writer: materialize a root frame + merged report in one go.

    Kept for tests and ad-hoc use. Live runs go through `_LiveReporter`.
    Writes into `{log_dir}/{invoke_id}/report.yaml`."""
    ctx = _InvocationContext(
        invoke_id=invoke_id, log_dir=log_dir, cwd=cwd,
        frame_key=_ROOT_FRAME_KEY, is_nested=False,
    )
    ctx.frames_dir.mkdir(parents=True, exist_ok=True)
    _atomic_yaml_write(ctx.frame_path(), {
        "frame_key": _ROOT_FRAME_KEY, "is_nested": False,
        "kind": kind, "tasks": list(tasks), "cwd": cwd,
        "started_at": started_at, "ended_at": ended_at,
        "tree": tree.to_dict(),
    })
    frames = _load_frames(ctx.frames_dir)
    doc = _build_merged_report(
        invoke_id=invoke_id, frames=frames,
        root_frame=frames[_ROOT_FRAME_KEY][0], ended_at=ended_at,
    )
    _atomic_yaml_write(ctx.report_path, doc)
    return ctx.report_path
