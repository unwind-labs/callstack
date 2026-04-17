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

import concurrent.futures as cf
import datetime as dt
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


CALL_TREE_SCHEMA_VERSION = "2"

from . import state as st
from .channel import Channel, TurnTimeout
from .protocol import parse_envelope
from .session import SessionLocator, SessionRef, count_lines
from .trace import TraceWriter, TreeStore


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
    # Peak `input_tokens` seen on this frame across all its turns. Updated
    # by the driver after each TurnResult — enables Fig-2-style parent-context
    # growth plots without re-parsing the trace JSONL.
    max_context_tokens_seen: int = 0

    # ---- denormalized for serialization / public API ----
    session_id: Optional[str] = None
    clone_path: Optional[str] = None
    result: Any = None
    summary: Optional[str] = None
    suggested_next: Optional[str] = None
    error: Optional[str] = None

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
            "id": self.id, "task": self.task,
            "state": _state_to_dict(self.state),
            "parent_lines": self.parent_lines, "duration": self.duration,
            "max_context_tokens_seen": self.max_context_tokens_seen,
            "session_id": self.session_id, "clone_path": self.clone_path,
            "result": self.result, "summary": self.summary,
            "suggested_next": self.suggested_next, "error": self.error,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Node":
        return cls(
            id=d["id"], task=d["task"],
            state=_state_from_dict(d["state"]),
            parent_lines=d.get("parent_lines", 0),
            duration=d.get("duration", 0.0),
            max_context_tokens_seen=d["max_context_tokens_seen"],
            session_id=d.get("session_id"), clone_path=d.get("clone_path"),
            result=d.get("result"), summary=d.get("summary"),
            suggested_next=d.get("suggested_next"), error=d.get("error"),
            children=[cls.from_dict(c) for c in d.get("children", [])],
        )


@dataclass
class Tree:
    """The full execution result."""
    root_session: SessionRef
    nodes: list[Node]                  # one per task
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
            root_session=SessionRef(session_id=d["root_session_id"],
                                    file=Path(d["root_session_file"])),
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


@dataclass
class Driver:
    channel: Channel
    locator: SessionLocator
    trace: TraceWriter
    store: TreeStore
    cwd: Optional[str] = None
    timeout: int = 300
    max_depth: int = 5
    # Opaque label recorded into traces for pass^k trial grouping. Does NOT
    # produce deterministic provider output — the Anthropic API has no seed
    # parameter as of 2026-04. See agent_callstack.call() docstring.
    seed: Optional[int] = None

    # ---- entry points ----

    def run(self, parent: SessionRef, tasks: list[str], base_depth: int = 0) -> Tree:
        """Execute one or more root tasks. Multiple tasks run in parallel."""
        nodes = [self._new_node(task) for task in tasks]
        tree = Tree(root_session=parent, nodes=nodes, base_depth=base_depth)

        if base_depth + 1 > self.max_depth:
            for n in nodes:
                n.state = st.Failed(error=f"Max call depth ({self.max_depth}) exceeded")
                n.error = n.state.error
            return tree

        if len(nodes) == 1:
            self._drive(nodes[0], parent.session_id, parent.file, base_depth + 1)
        else:
            with cf.ThreadPoolExecutor(max_workers=len(nodes)) as pool:
                futures = [
                    pool.submit(self._drive, n, parent.session_id, parent.file, base_depth + 1)
                    for n in nodes
                ]
                cf.wait(futures)

        self._persist_if_yielded(tree)
        return tree

    def resume(self, tree: Tree, target_session_id: str, reply: str) -> Tree:
        """Resume the leaf identified by `target_session_id` with the user's reply."""
        leaf = tree.find_by_session(target_session_id)
        if leaf is None or not leaf.is_yielded_directly:
            raise RuntimeError(f"No yielded node with session_id={target_session_id}")

        # Walk up to find this leaf's depth.
        depth = self._depth_of(tree, leaf)
        parent_file = self._parent_file_for(tree, leaf)

        # Step the leaf forward with UserReplied, then continue driving until
        # it terminates or yields again. If it terminates, propagate up.
        self._continue(leaf, st.UserReplied(reply=reply), depth, parent_file)
        self._propagate_up(tree, leaf)
        self._persist_if_yielded(tree)
        return tree

    # ---- private: tree topology helpers ----

    def _new_node(self, task: str) -> Node:
        nid = uuid.uuid4().hex
        return Node(id=nid, task=task,
                    state=st.Pending(parent_session_id="", task=task, task_id=nid[:8]))

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
        events to each ancestor that is AwaitingChild on it."""
        node = leaf
        while True:
            parent = self._find_parent(tree, node)
            if parent is None:
                return
            if not isinstance(parent.state, st.AwaitingChild):
                return
            if isinstance(node.state, st.Done):
                event: st.Event = st.ChildDone(
                    child_id=parent.state.child_id, result=node.state.result,
                )
            elif isinstance(node.state, st.Failed):
                event = st.ChildFailed(
                    child_id=parent.state.child_id, error=node.state.error,
                )
            else:
                # Parent stays parked; nothing to do.
                return
            depth = self._depth_of(tree, parent)
            parent_file = self._parent_file_for(tree, parent)
            self._continue(parent, event, depth, parent_file)
            node = parent

    @staticmethod
    def _find_parent(tree: Tree, target: Node) -> Optional[Node]:
        def walk(n: Node) -> Optional[Node]:
            for c in n.children:
                if c is target:
                    return n
                hit = walk(c)
                if hit is not None:
                    return hit
            return None
        for root in tree.nodes:
            hit = walk(root)
            if hit is not None:
                return hit
        return None

    def _persist_if_yielded(self, tree: Tree) -> None:
        for leaf in tree.yielded_leaves():
            if leaf.clone_path:
                self.store.save(Path(leaf.clone_path), tree.to_dict())

    # ---- private: state machine driver ----

    def _drive(self, node: Node, parent_session_id: str,
               parent_session_file: Path, depth: int) -> None:
        """Drive `node` from Pending until it terminates, yields, or its current
        child yields. May recurse to drive children synchronously."""
        node.parent_lines = count_lines(parent_session_file)
        node.state = st.Pending(parent_session_id=parent_session_id,
                                task=node.task, task_id=node.id[:8])
        self._continue(node, st.Start(), depth, parent_session_file)

    def _continue(self, node: Node, initial_event: st.Event,
                  depth: int, parent_file: Path) -> None:
        """Step `node` until terminal, suspended, or blocked on a yielded child."""
        event: Optional[st.Event] = initial_event
        while event is not None:
            new_state, effects = st.step(node.state, event)
            node.state = new_state
            _denormalize(node)

            if st.is_terminal(new_state) or st.is_suspended(new_state):
                return
            if not effects:
                return

            assert len(effects) == 1, "step() should produce at most one effect"
            event = self._perform(effects[0], node, depth, parent_file)
            if event is None:
                # Effect signalled "we're now blocked downstream" — stop.
                return

    def _perform(self, effect: st.Effect, node: Node,
                 depth: int, parent_file: Path) -> Optional[st.Event]:
        """Run an effect and return the resulting event (or None if blocked)."""
        if isinstance(effect, st.RunTurn):
            return self._run_turn(effect, node, depth, parent_file)
        if isinstance(effect, st.SpawnChild):
            return self._spawn_child(effect, node, depth)
        raise TypeError(f"unknown effect: {effect!r}")

    def _run_turn(self, effect: st.RunTurn, node: Node,
                  depth: int, parent_file: Path) -> st.Event:
        t0 = time.time()
        started_at = _utc_now()
        try:
            result = self.channel.run_turn(
                effect.source_session_id, effect.prompt,
                fork=effect.fork, cwd=self.cwd, timeout=self.timeout,
            )
        except TurnTimeout as e:
            node.duration += time.time() - t0
            self.trace.write(
                depth=depth, task=node.task,
                session_id=node.session_id or "unknown",
                result=e.partial, duration=node.duration,
                api_request_id="", input_tokens=0, output_tokens=0,
                cache_read_tokens=0, cache_creation_tokens=0,
                started_at_utc=started_at, ended_at_utc=_utc_now(),
                seed=self.seed, error=str(e),
            )
            return st.TurnFailed(error=str(e), partial=e.partial)
        except Exception as e:
            node.duration += time.time() - t0
            self.trace.write(
                depth=depth, task=node.task,
                session_id=node.session_id or "unknown",
                result="", duration=node.duration,
                api_request_id="", input_tokens=0, output_tokens=0,
                cache_read_tokens=0, cache_creation_tokens=0,
                started_at_utc=started_at, ended_at_utc=_utc_now(),
                seed=self.seed, error=str(e),
            )
            return st.TurnFailed(error=f"Invocation failed: {e}")

        node.duration += result.duration or (time.time() - t0)
        if result.input_tokens > node.max_context_tokens_seen:
            node.max_context_tokens_seen = result.input_tokens
        # Resolve the clone path right after the fork completes.
        if effect.fork:
            resolved = self.locator.resolve(result.session_id, cwd=self.cwd)
            if resolved is not None:
                node.clone_path = str(resolved)
        self.trace.write(
            depth=depth, task=node.task, session_id=result.session_id,
            result=result.text, duration=node.duration,
            api_request_id=result.api_request_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_read_tokens=result.cache_read_tokens,
            cache_creation_tokens=result.cache_creation_tokens,
            started_at_utc=started_at, ended_at_utc=_utc_now(),
            seed=self.seed,
        )
        return st.TurnCompleted(envelope=parse_envelope(result.text),
                                session_id=result.session_id)

    def _spawn_child(self, effect: st.SpawnChild, node: Node,
                     depth: int) -> Optional[st.Event]:
        if depth + 1 > self.max_depth:
            return st.ChildFailed(
                child_id=node.state.child_id if isinstance(node.state, st.AwaitingChild) else "",
                error=f"Max call depth ({self.max_depth}) exceeded",
            )

        child = self._new_node(effect.task)
        node.children.append(child)
        # Children fork from this node's clone, not the original parent's session.
        child_parent_file = Path(node.clone_path) if node.clone_path else Path(".")
        self._drive(child, effect.parent_session_id, child_parent_file, depth + 1)

        if isinstance(child.state, st.Done):
            return st.ChildDone(
                child_id=node.state.child_id if isinstance(node.state, st.AwaitingChild) else "",
                result=child.state.result,
            )
        if isinstance(child.state, st.Failed):
            return st.ChildFailed(
                child_id=node.state.child_id if isinstance(node.state, st.AwaitingChild) else "",
                error=child.state.error,
            )
        # Child suspended (yielded) — parent is now blocked downstream.
        return None


# ---------- internal serialization & helpers ----------

_STATE_TYPES = {
    "pending": st.Pending,
    "awaiting_turn": st.AwaitingTurn,
    "awaiting_child": st.AwaitingChild,
    "awaiting_user": st.AwaitingUser,
    "done": st.Done,
    "failed": st.Failed,
}


def _state_to_dict(s: st.State) -> dict:
    return {**s.__dict__, "kind": s.kind}


def _state_from_dict(d: dict) -> st.State:
    cls = _STATE_TYPES[d["kind"]]
    args = {k: v for k, v in d.items() if k != "kind"}
    return cls(**args)


def _status_label(s: st.State) -> str:
    return {
        "pending": "pending",
        "awaiting_turn": "running",
        "awaiting_child": "running",
        "awaiting_user": "yielded",
        "done": "complete",
        "failed": "error",
    }[s.kind]


def _denormalize(node: Node) -> None:
    """Mirror the state's session id/result/error onto the Node's flat fields."""
    s = node.state
    if isinstance(s, st.AwaitingTurn) and s.session_id:
        node.session_id = s.session_id
    elif isinstance(s, (st.AwaitingChild, st.AwaitingUser)):
        node.session_id = s.session_id
    elif isinstance(s, st.Done):
        if s.session_id:
            node.session_id = s.session_id
        node.result = s.result
        node.summary = s.summary
        node.suggested_next = s.suggested_next
    elif isinstance(s, st.Failed):
        if s.session_id:
            node.session_id = s.session_id
        node.error = s.error


def _find(root: Node, session_id: str) -> Optional[Node]:
    if root.session_id == session_id:
        return root
    for c in root.children:
        hit = _find(c, session_id)
        if hit is not None:
            return hit
    return None
