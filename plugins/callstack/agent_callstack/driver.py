"""The driver: turns Effects into real I/O and feeds Events back into step().

A `Node` is a mutable wrapper that records the current `State` plus
denormalized fields (session id, clone path, accumulated duration) for
inspection. Transitions are still pure — the driver just performs the
effects and feeds the resulting events back through `step()`.

`Driver.run(parent, tasks)` executes one or more root tasks and returns the
finished `Tree`. Multiple tasks fan out via a thread pool. Children execute
synchronously within their parent's drive call. When a node yields, the
whole subtree pauses; `Driver.resume(reply)` continues it.
"""

from __future__ import annotations

import atexit
import concurrent.futures as cf
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Optional, cast

from . import state as st
from .channel import (
    Channel,
    TurnTimeout,
)
from .invocation_ctx import _utc_now_iso as _utc_now
from .protocol import parse_envelope
from .session import SessionRef, count_lines
from .trace import TraceWriter, TreeStore

CALL_TREE_SCHEMA_VERSION = "2"

# Worker pool size for `call_many` fan-out and nested driving. Matches
# the prior `2 × CALLSTACK_MAX_CONCURRENT_FORKS` derivation (16) so the
# concurrent-driving capacity is unchanged from before the cap removal.
_RUN_POOL_MAX_WORKERS = 16


# PERF-J: single module-level pool sized to the channel's in-flight turn
# cap. Drives multi-task call_many fan-out without paying the cold-pool
# construction cost per call. atexit shuts it down politely so pending
# futures aren't killed mid-write.
_RUN_POOL: Optional[cf.ThreadPoolExecutor] = None
_RUN_POOL_LOCK = threading.Lock()


def _get_run_pool() -> cf.ThreadPoolExecutor:
    global _RUN_POOL
    if _RUN_POOL is None:
        with _RUN_POOL_LOCK:
            if _RUN_POOL is None:
                _RUN_POOL = cf.ThreadPoolExecutor(
                    max_workers=_RUN_POOL_MAX_WORKERS,
                    thread_name_prefix="callstack-driver",
                )
                atexit.register(_shutdown_run_pool)
    return _RUN_POOL


def _shutdown_run_pool() -> None:
    global _RUN_POOL
    pool, _RUN_POOL = _RUN_POOL, None
    if pool is not None:
        pool.shutdown(wait=True, cancel_futures=False)


def _classify_upstream_failure(text: str) -> Optional[str]:
    """If `text` is a Claude Code synthetic upstream-rate-limit message,
    return a typed error string for `st.TurnFailed`. Otherwise None.

    Anchored on `"API Error"` so the signature isn't matched in normal
    assistant prose. When a second synthetic shows up in traces, this
    grows back into a table — until then a direct check is clearer than
    a one-row dispatcher."""
    if "API Error" in text and "Server is temporarily limiting requests" in text:
        return f"upstream_rate_limited: {text.strip()[:500]}"
    return None


# ---------- Tree of execution nodes ----------


@dataclass
class Node:
    """One agent invocation. Mutable wrapper around the pure State."""

    id: str
    task: str
    state: st.State
    parent_lines: int = 0
    duration: float = 0.0
    children: list["Node"] = field(default_factory=list)
    # Peak effective context size (input_tokens + cache_read_tokens) seen on
    # this frame across all its turns. Cache reads are counted because they
    # *are* in the model's context on that turn — the input/cache_read split
    # is pricing metadata, not a context-size distinction. Fig 2's
    # parent-context growth plot reads this field directly.
    max_context_tokens_seen: int = 0
    # How this node's session was launched. Surfaced in report.yaml so the
    # Unwind UI can render distinct icons for fork vs fresh vs cross-project.
    #   "fork"                — `--resume <parent> --fork-session` (inherits)
    #   "fresh"               — brand-new session in the parent's project folder
    #   "fresh_cross_project" — brand-new session in a different project folder
    # Nested children spawned via the agent's CALL envelope always inherit
    # the parent node's session via fork semantics, so they're always "fork".
    call_type: str = "fork"

    # clone_path is a genuine on-disk fact (the resolved session JSONL), not
    # derivable from `state` — so it stays a real field.
    clone_path: Optional[str] = None

    # ---- read-through views over `state` (single source of truth) ----
    # These were previously denormalized flat fields kept in sync by
    # `_denormalize`. Deriving them from `state` removes the second source of
    # truth (and the desync the old `_early_session` callback could cause):
    # `step()` is the only writer of `state`, so these can never drift.
    @property
    def session_id(self) -> Optional[str]:
        return getattr(self.state, "session_id", None)

    @property
    def result(self) -> Any:
        return self.state.result if isinstance(self.state, st.Done) else None

    @property
    def summary(self) -> Optional[str]:
        return self.state.summary if isinstance(self.state, st.Done) else None

    @property
    def suggested_next(self) -> Optional[str]:
        return self.state.suggested_next if isinstance(self.state, st.Done) else None

    @property
    def error(self) -> Optional[str]:
        # Failed / Timeout / Abandoned all carry `error`.
        return getattr(self.state, "error", None)

    @property
    def status(self) -> str:
        return _status_label(self.state)

    @property
    def is_yielded_directly(self) -> bool:
        return isinstance(self.state, st.AwaitingUser)

    def yielded_descendant(self) -> Optional["Node"]:
        """The leaf node in AwaitingUser anywhere under this subtree."""
        if self.is_yielded_directly:
            return self
        for c in self.children:
            found = c.yielded_descendant()
            if found is not None:
                return found
        return None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task": self.task,
            "state": _state_to_dict(self.state),
            "parent_lines": self.parent_lines,
            "duration": self.duration,
            "max_context_tokens_seen": self.max_context_tokens_seen,
            "call_type": self.call_type,
            "session_id": self.session_id,
            "clone_path": self.clone_path,
            "result": self.result,
            "summary": self.summary,
            "suggested_next": self.suggested_next,
            "error": self.error,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Node":
        return cls(
            id=d["id"],
            task=d["task"],
            state=_state_from_dict(d["state"]),
            parent_lines=d.get("parent_lines", 0),
            duration=d.get("duration", 0.0),
            max_context_tokens_seen=d.get("max_context_tokens_seen", 0),
            call_type=d.get("call_type", "fork"),
            # session_id/result/summary/suggested_next/error are now derived
            # from `state` (see the read-through properties above); the dict
            # still carries them for human/UI consumers, but we reconstruct
            # them from `state` on load — making the serialized flat fields
            # advisory, not authoritative.
            clone_path=d.get("clone_path"),
            children=[cls.from_dict(c) for c in d.get("children", [])],
        )


@dataclass
class Tree:
    """The full execution result."""

    root_session: SessionRef
    nodes: list[Node]  # one per task
    base_depth: int

    def to_dict(self) -> dict:
        return {
            "schema_version": CALL_TREE_SCHEMA_VERSION,
            "root_session_id": self.root_session.session_id,
            "root_session_file": str(self.root_session.file),
            "base_depth": self.base_depth,
            "nodes": [n.to_dict() for n in self.nodes],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Tree":
        schema = d.get("schema_version")
        if schema != CALL_TREE_SCHEMA_VERSION:
            raise ValueError(
                f".call_tree schema_version={schema!r} is not supported; "
                f"expected {CALL_TREE_SCHEMA_VERSION!r}. Snapshots written "
                f"before paper-v1-rc1 (schema v1) cannot be resumed."
            )
        return cls(
            root_session=SessionRef(session_id=d["root_session_id"], file=Path(d["root_session_file"])),
            nodes=[Node.from_dict(n) for n in d["nodes"]],
            base_depth=d.get("base_depth", 0),
        )

    def yielded_leaves(self) -> list[Node]:
        out: list[Node] = []
        for root in self.nodes:
            leaf = root.yielded_descendant()
            if leaf:
                out.append(leaf)
        return out

    def find_by_session(self, session_id: str) -> Optional[Node]:
        for root in self.nodes:
            found = _find(root, session_id)
            if found:
                return found
        return None


# ---------- Driver ----------


class MaxDepthExceeded(Exception):
    pass


SessionResolver = Callable[[str, Optional[str]], Optional[Path]]


@dataclass
class Driver:
    channel: Channel
    # Resolves a session_id (and optional cwd) to its .jsonl file path.
    # Matches `SessionLocator.resolve(session_id, cwd=...)` so existing
    # callers can pass `SessionLocator().resolve` directly. Decoupling
    # the Driver from session discovery (ARCH-9) keeps the driver
    # testable with a trivial lambda.
    resolve_session: SessionResolver
    trace: TraceWriter
    store: TreeStore
    cwd: Optional[str] = None
    timeout: int = 300
    max_depth: int = 10
    # Opaque label recorded into traces for pass^k trial grouping. Does NOT
    # produce deterministic provider output — the Anthropic API has no seed
    # parameter as of 2026-04. See agent_callstack.call() docstring.
    seed: Optional[int] = None
    # Invoked after every node-state transition with the current tree so
    # observers (e.g. the YAML report writer) can snapshot progress live.
    # Must be cheap — it runs on the driving thread, and for `call_many`
    # is called concurrently from each root's worker thread. Failures are
    # swallowed so a broken reporter can't kill execution.
    on_progress: Optional[Callable[["Tree"], None]] = None

    # Most recently constructed/resumed Tree; written by `run` and
    # `resume` immediately after the Tree object exists. Exposed as a
    # public attribute (not underscore-prefixed) so `Caller.run`'s
    # finally-block fallback can seal a partial report when `run`
    # raises after the tree exists but before returning — the fallback
    # is the only consumer outside the driver itself.
    last_tree: Optional["Tree"] = field(default=None, init=False, repr=False)
    # SEC-011: log the first on_progress failure with full traceback, then
    # set this flag so subsequent ticks swallow silently. Avoids drowning
    # stderr in identical errors on every state transition while still
    # surfacing the first occurrence for debugging.
    _notify_failed: bool = field(default=False, init=False, repr=False)
    # CORR-104: log the first sibling-task exception in `call_many` with
    # full traceback, suppress subsequent occurrences. Same first-occurrence
    # policy as `_notify_failed` so a wave of similar resolver failures
    # doesn't drown stderr.
    _sibling_exception_logged: bool = field(default=False, init=False, repr=False)
    # CONC-3: serializes `_propagate_up` so concurrent producers of
    # ChildDone/ChildFailed can't double-step the same ancestor. Today
    # `_propagate_up` is only reached from `resume()` and the recursive
    # `_spawn_child` path is single-threaded, but the lock is cheap
    # defense-in-depth for future code paths (parallel resume of
    # sibling yields, for example). RLock so a re-entry from
    # `_continue → _perform → _spawn_child` inside the propagate loop
    # doesn't self-deadlock.
    _propagate_lock: threading.RLock = field(
        default_factory=threading.RLock,
        init=False,
        repr=False,
    )

    # ---- entry points ----

    def run(self, parent: SessionRef, tasks: list[str], base_depth: int = 0, context: str = "fork") -> Tree:
        """Execute one or more root tasks. Multiple tasks run in parallel.

        context — how root tasks launch their underlying claude session:
            "fork"  (default) — inherit the parent's transcript via
                                `--resume + --fork-session`.
            "fresh"           — brand-new session, no inherited context."""
        if context not in ("fork", "fresh"):
            raise ValueError(f"invalid context: {context!r}")
        call_type = self._derive_call_type(context, parent)
        nodes = [self._new_node(task, context_mode=context, call_type=call_type) for task in tasks]
        tree = Tree(root_session=parent, nodes=nodes, base_depth=base_depth)
        self.last_tree = tree
        self._notify()

        if base_depth + 1 > self.max_depth:
            for n in nodes:
                n.state = st.Failed(error=f"Max call depth ({self.max_depth}) exceeded")
            self._notify()
            return tree

        if len(nodes) == 1:
            self._drive(nodes[0], parent.session_id, parent.file, base_depth + 1)
        else:
            # PERF-J: reuse the module-level pool instead of constructing a
            # fresh ThreadPoolExecutor per call_many.
            pool = _get_run_pool()
            futures = [(n, pool.submit(self._drive, n, parent.session_id, parent.file, base_depth + 1)) for n in nodes]
            cf.wait([fut for _n, fut in futures])
            # CORR-104: collect exceptions per-future so one sibling's
            # unexpected error doesn't strand the others' results.
            # `_drive` doesn't raise on normal node failures (those land in
            # Node.state); an exception here means something deeper broke
            # (resolver, OSError, etc.). Mark the node as Failed and keep
            # going — the caller still gets siblings' real results.
            for n, fut in futures:
                exc = fut.exception()
                if exc is None:
                    continue
                err_msg = f"{type(exc).__name__}: {exc}"
                # Preserve any state the node already reached (e.g. it had
                # completed before a stray exception slipped past) by only
                # overwriting non-terminal states.
                if not st.is_terminal(n.state):
                    n.state = st.Failed(error=err_msg)
                if not self._sibling_exception_logged:
                    import sys as _sys
                    import traceback as _tb

                    print(
                        f"[callstack] sibling task raised (further occurrences suppressed): {err_msg}",
                        file=_sys.stderr,
                    )
                    _tb.print_exception(type(exc), exc, exc.__traceback__, file=_sys.stderr)
                    self._sibling_exception_logged = True

        self._persist_if_yielded(tree)
        self._notify()
        return tree

    def _derive_call_type(self, context: str, parent: SessionRef) -> str:
        """Map (context, parent project folder, self.cwd) → call_type label."""
        if context == "fork":
            return "fork"
        # context == "fresh"
        parent_dir = parent.cwd or ""
        own_dir = self.cwd or ""
        try:
            same = parent_dir and own_dir and os.path.realpath(parent_dir) == os.path.realpath(own_dir)
        except OSError:
            same = False
        return "fresh" if same or not own_dir or not parent_dir else "fresh_cross_project"

    def resume(self, tree: Tree, target_session_id: str, reply: str) -> Tree:
        """Resume the leaf identified by `target_session_id` with the user's reply."""
        leaf = tree.find_by_session(target_session_id)
        if leaf is None or not leaf.is_yielded_directly:
            raise RuntimeError(f"No yielded node with session_id={target_session_id}")

        self.last_tree = tree
        self._notify()

        # Walk up to find this leaf's depth.
        depth = self._depth_of(tree, leaf)
        parent_file = self._parent_file_for(tree, leaf)

        # Step the leaf forward with UserReplied, then continue driving until
        # it terminates or yields again. If it terminates, propagate up.
        self._continue(leaf, st.UserReplied(reply=reply), depth, parent_file)
        self._propagate_up(tree, leaf)
        self._persist_if_yielded(tree)
        self._notify()
        return tree

    # ---- private: progress notification ----

    def _notify(self) -> None:
        """Fire on_progress with the current tree. Never raises."""
        if self.on_progress is None or self.last_tree is None:
            return
        try:
            self.on_progress(self.last_tree)
        except Exception:
            if not self._notify_failed:
                # First failure: surface with traceback so the cause is
                # debuggable. Subsequent failures stay quiet (the run can
                # produce thousands of transitions; one bad reporter would
                # otherwise spam stderr).
                import sys
                import traceback

                print("[callstack] on_progress callback raised (further failures suppressed):", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                self._notify_failed = True

    # ---- private: tree topology helpers ----

    def _new_node(self, task: str, *, context_mode: str = "fork", call_type: str = "fork") -> Node:
        nid = uuid.uuid4().hex
        cmode = cast(Literal["fork", "fresh"], context_mode)
        return Node(
            id=nid,
            task=task,
            call_type=call_type,
            state=st.Pending(parent_session_id="", task=task, task_id=nid[:8], context_mode=cmode),
        )

    def _depth_of(self, tree: Tree, target: Node) -> int:
        def walk(n: Node, d: int) -> Optional[int]:
            if n is target:
                return d
            for c in n.children:
                hit = walk(c, d + 1)
                if hit is not None:
                    return hit
            return None

        for root in tree.nodes:
            d = walk(root, tree.base_depth + 1)
            if d is not None:
                return d
        return tree.base_depth + 1

    def _parent_file_for(self, tree: Tree, target: Node) -> Path:
        """Session file of `target`'s parent (or root session for top-level)."""

        def find_parent(n: Node) -> Optional[Node]:
            for c in n.children:
                if c is target:
                    return n
                found = find_parent(c)
                if found is not None:
                    return found
            return None

        for root in tree.nodes:
            if root is target:
                return tree.root_session.file
            p = find_parent(root)
            if p and p.clone_path:
                return Path(p.clone_path)
        return tree.root_session.file

    def _propagate_up(self, tree: Tree, leaf: Node) -> None:
        """Walk up from a just-completed leaf, feeding ChildDone/ChildFailed
        events to each ancestor that is AwaitingChild on it.

        Ancestor lookups go through a one-shot index built at entry so the
        loop is O(depth) instead of O(depth · N) (ARCH-3).

        CONC-3: serialized via ``self._propagate_lock`` so two threads
        that both observe ``AwaitingChild`` on the same ancestor can't
        double-step it. Today only `resume()` reaches this method and
        the recursive `_spawn_child` path is single-threaded; the lock
        is cheap defense-in-depth for future parallel-resume paths."""
        with self._propagate_lock:
            index = _TreeIndex.build(tree)
            node = leaf
            while True:
                parent = index.parent_of.get(id(node))
                if parent is None:
                    return
                if not isinstance(parent.state, st.AwaitingChild):
                    return
                if isinstance(node.state, st.Done):
                    event: st.Event = st.ChildDone(
                        child_id=parent.state.child_id,
                        result=node.state.result,
                    )
                elif isinstance(node.state, st.Failed):
                    event = st.ChildFailed(
                        child_id=parent.state.child_id,
                        error=node.state.error,
                    )
                else:
                    # Parent stays parked; nothing to do.
                    return
                depth = index.depth_of[id(parent)]
                parent_file = index.parent_file_of[id(parent)]
                self._continue(parent, event, depth, parent_file)
                node = parent

    def _persist_if_yielded(self, tree: Tree) -> None:
        for leaf in tree.yielded_leaves():
            if leaf.clone_path:
                self.store.save(Path(leaf.clone_path), tree.to_dict())

    # ---- private: state machine driver ----

    def _drive(self, node: Node, parent_session_id: str, parent_session_file: Path, depth: int) -> None:
        """Drive `node` from Pending until it terminates, yields, or its current
        child yields. May recurse to drive children synchronously."""
        # Preserve the node's pre-set context_mode (stamped at _new_node time
        # for root nodes; defaults to "fork" for children spawned via
        # CALL envelopes). Fresh nodes have no inherited transcript so
        # parent_lines stays at 0.
        prior = node.state
        cmode: Literal["fork", "fresh"] = prior.context_mode if isinstance(prior, st.Pending) else "fork"
        if cmode == "fresh":
            node.parent_lines = 0
        else:
            node.parent_lines = count_lines(parent_session_file)
        node.state = st.Pending(
            parent_session_id=parent_session_id, task=node.task, task_id=node.id[:8], context_mode=cmode
        )
        self._continue(node, st.Start(), depth, parent_session_file)

    def _continue(self, node: Node, initial_event: st.Event, depth: int, parent_file: Path) -> None:
        """Step `node` until terminal, suspended, or blocked on a yielded child."""
        event: Optional[st.Event] = initial_event
        while event is not None:
            new_state, effects = st.step(node.state, event)
            node.state = new_state
            self._notify()

            if st.is_terminal(new_state) or st.is_suspended(new_state):
                return
            if not effects:
                return

            assert len(effects) == 1, "step() should produce at most one effect"
            event = self._perform(effects[0], node, depth, parent_file)
            if event is None:
                # Effect signalled "we're now blocked downstream" — stop.
                return

    def _perform(self, effect: st.Effect, node: Node, depth: int, parent_file: Path) -> Optional[st.Event]:
        """Run an effect and return the resulting event (or None if blocked)."""
        if isinstance(effect, st.RunTurn):
            return self._run_turn(effect, node, depth, parent_file)
        if isinstance(effect, st.SpawnChild):
            return self._spawn_child(effect, node, depth)
        raise TypeError(f"unknown effect: {effect!r}")

    def _run_turn(self, effect: st.RunTurn, node: Node, depth: int, parent_file: Path) -> st.Event:
        t0 = time.time()
        started_at = _utc_now()
        try:
            # Stamp this forked subprocess with its node id so a nested
            # MCP invoke launched from inside it can deterministically
            # identify its own frame (beats session-id heuristics).
            # When a forked turn (i.e. first turn for this node) reports its
            # session_id mid-stream — typically via claude's `system init`
            # message at the very start — propagate it to the node and fire
            # _notify so progress observers (LiveReporter / report.yaml) see
            # the new session id WITHOUT waiting for the full turn to finish.
            # This matters for long first turns (e.g. /task-c which then
            # spawns deeper children before returning).
            # Both "fork" and "fresh" produce a NEW session id (reported by
            # claude's `system init`). "resume" continues an existing one and
            # the id is already set on the node.
            produces_new_session = effect.mode in ("fork", "fresh")

            # Pre-allocate the child's session UUID for fork/fresh so the
            # child claude is told (via `--session-id`) exactly which
            # UUID to use, and its MCP server can read the same value
            # back from CALLSTACK_OWN_SESSION env. Removes the
            # SessionLocator mtime-fallback race on concurrent siblings.
            preallocated_sid: Optional[str] = str(uuid.uuid4()) if produces_new_session else None

            def _early_session(sid: str) -> None:
                if not produces_new_session:
                    return
                if node.session_id == sid:
                    return
                # node.session_id is now derived from node.state, so the
                # single write is the AwaitingTurn rewrite — the derived
                # property follows it. (Previously this set node.session_id
                # AND node.state, the desync the property migration removes.)
                if isinstance(node.state, st.AwaitingTurn) and node.state.session_id != sid:
                    node.state = st.AwaitingTurn(session_id=sid)
                self._notify()

            result = self.channel.run_turn(
                effect.source_session_id,
                effect.prompt,
                mode=effect.mode,
                cwd=self.cwd,
                timeout=self.timeout,
                extra_env=({"CALLSTACK_FRAME_KEY": node.id} if produces_new_session else None),
                on_session_id=_early_session,
                preallocated_session_id=preallocated_sid,
            )
            # NB: the consistency check (claude must honor --session-id)
            # lives inside ClaudeChannel itself, NOT here. The Driver
            # is channel-agnostic and ScriptedChannel doesn't simulate
            # the --session-id contract — enforcing it at the Driver
            # would break every scripted test that returns a stable
            # known session id like "child-1". See `_run_one_turn` in
            # channel.py for the production-only check.
        except TurnTimeout as e:
            node.duration += time.time() - t0
            self.trace.write(
                depth=depth,
                task=node.task,
                session_id=node.session_id or "unknown",
                result=e.partial,
                duration=node.duration,
                api_request_id="",
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                started_at_utc=started_at,
                ended_at_utc=_utc_now(),
                seed=self.seed,
                error=str(e),
            )
            return st.TurnFailed(error=str(e), partial=e.partial)
        except Exception as e:
            node.duration += time.time() - t0
            self.trace.write(
                depth=depth,
                task=node.task,
                session_id=node.session_id or "unknown",
                result="",
                duration=node.duration,
                api_request_id="",
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                started_at_utc=started_at,
                ended_at_utc=_utc_now(),
                seed=self.seed,
                error=str(e),
            )
            return st.TurnFailed(error=f"Invocation failed: {e}")

        node.duration += result.duration or (time.time() - t0)
        # Peak effective context = uncached input + cache reads. Both are
        # tokens the model reasoned from this turn; the split is pricing,
        # not context size. Fig 2 plots this quantity.
        effective_context = result.input_tokens + result.cache_read_tokens
        if effective_context > node.max_context_tokens_seen:
            node.max_context_tokens_seen = effective_context
        # Resolve the clone path right after the new session lands (fork or
        # fresh). For fresh + cross-project, the new session lives in the
        # child's cwd's project dir — pass the effective cwd so the locator
        # looks in the right place.
        if produces_new_session:
            resolved = self.resolve_session(result.session_id, self.cwd)
            if resolved is not None:
                node.clone_path = str(resolved)
        self.trace.write(
            depth=depth,
            task=node.task,
            session_id=result.session_id,
            result=result.text,
            duration=node.duration,
            api_request_id=result.api_request_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_read_tokens=result.cache_read_tokens,
            cache_creation_tokens=result.cache_creation_tokens,
            started_at_utc=started_at,
            ended_at_utc=_utc_now(),
            seed=self.seed,
        )
        envelope = parse_envelope(result.text)
        if envelope is None:
            # No parseable envelope — child crashed mid-thought, was cut off,
            # or emitted an unknown opcode. Before surfacing the generic
            # failure, see if the text is a recognized synthetic from Claude
            # Code (e.g. upstream rate-limit) so the parent agent gets an
            # actionable typed error instead of a vague "no envelope".
            classified = _classify_upstream_failure(result.text)
            if classified is not None:
                return st.TurnFailed(
                    error=classified,
                    session_id=result.session_id,
                    partial=result.text,
                )
            return st.TurnFailed(
                error="child emitted no parseable envelope",
                session_id=result.session_id,
                partial=result.text,
            )
        return st.TurnCompleted(envelope=envelope, session_id=result.session_id)

    def _spawn_child(self, effect: st.SpawnChild, node: Node, depth: int) -> Optional[st.Event]:
        # SpawnChild is only emitted by the state machine from the AwaitingChild
        # transition (state.py: AwaitingTurn + TurnCompleted(Call) -> AwaitingChild
        # + [SpawnChild]), so the parent node is always AwaitingChild here and
        # carries the child_id every returned event must echo back. Pin the
        # invariant once instead of re-deriving it (with a dead else-branch)
        # at each of the three return sites below.
        assert isinstance(node.state, st.AwaitingChild), "SpawnChild dispatched against non-AwaitingChild parent"
        child_id = node.state.child_id

        if depth + 1 > self.max_depth:
            return st.ChildFailed(
                child_id=child_id,
                error=f"Max call depth ({self.max_depth}) exceeded",
            )

        child = self._new_node(effect.task)
        node.children.append(child)
        # Children fork from this node's clone, not the original parent's session.
        child_parent_file = Path(node.clone_path) if node.clone_path else Path(".")
        self._drive(child, effect.parent_session_id, child_parent_file, depth + 1)

        if isinstance(child.state, st.Done):
            return st.ChildDone(child_id=child_id, result=child.state.result)
        if isinstance(child.state, st.Failed):
            return st.ChildFailed(child_id=child_id, error=child.state.error)
        # Child suspended (yielded) — parent is now blocked downstream.
        return None


# ---------- ancestor index (ARCH-3) ----------


@dataclass(frozen=True)
class _TreeIndex:
    """One-shot ancestor index for `_propagate_up`.

    Built by a single DFS over `tree.nodes`; replaces independent
    O(N) ancestor walks (`_depth_of`, `_parent_file_for`) per loop
    iteration with O(1) dict lookups. Keyed by `id(Node)` because Node
    is mutable and not hashable. Lifetime is the propagate call only;
    don't cache on Tree (children are appended during execution and
    invalidation would silently rot)."""

    parent_of: dict[int, Optional[Node]]
    depth_of: dict[int, int]
    parent_file_of: dict[int, Path]

    @classmethod
    def build(cls, tree: "Tree") -> "_TreeIndex":
        parent_of: dict[int, Optional[Node]] = {}
        depth_of: dict[int, int] = {}
        parent_file_of: dict[int, Path] = {}
        root_file = tree.root_session.file
        # Iterative DFS — deep linear stacks can blow Python's recursion limit.
        stack: list[tuple[Node, Optional[Node], int, Path]] = [
            (n, None, tree.base_depth + 1, root_file) for n in tree.nodes
        ]
        while stack:
            node, parent, depth, pfile = stack.pop()
            parent_of[id(node)] = parent
            depth_of[id(node)] = depth
            parent_file_of[id(node)] = pfile
            # Children fork from this node's clone, not its grandparent's.
            # When clone_path is missing (e.g. node failed before its snapshot
            # was resolved) we cannot honestly say "child forked from node",
            # so fall back to root_file — same sentinel the legacy
            # `_parent_file_for` returns. Falling back to `pfile` would
            # silently attribute the child to its grandparent's clone.
            child_pfile = Path(node.clone_path) if node.clone_path else root_file
            for c in node.children:
                stack.append((c, node, depth + 1, child_pfile))
        return cls(parent_of=parent_of, depth_of=depth_of, parent_file_of=parent_file_of)


# ---------- internal serialization & helpers ----------

_STATE_TYPES = {
    "pending": st.Pending,
    "awaiting_turn": st.AwaitingTurn,
    "awaiting_child": st.AwaitingChild,
    "awaiting_user": st.AwaitingUser,
    "done": st.Done,
    "failed": st.Failed,
    "timeout": st.Timeout,
    "abandoned": st.Abandoned,
}


def _state_to_dict(s: st.State) -> dict:
    return {**s.__dict__, "kind": s.kind}


def _state_from_dict(d: dict) -> st.State:
    cls = _STATE_TYPES[d["kind"]]
    args = {k: v for k, v in d.items() if k != "kind"}
    return cls(**args)


def _status_label(s: st.State) -> str:
    """Back-compat one-liner. Canonical mapping lives in state.status_label."""
    return st.status_label(s)


def _find(root: Node, session_id: str) -> Optional[Node]:
    if root.session_id == session_id:
        return root
    for c in root.children:
        hit = _find(c, session_id)
        if hit is not None:
            return hit
    return None
