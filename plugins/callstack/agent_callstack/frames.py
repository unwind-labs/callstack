"""Frame loading + merge helpers for the invocation report.

Each invocation writes a YAML frame file to `_frames/{key}.yaml` carrying
its Tree. The merged `report.yaml` is built by scanning every frame and
grafting non-root frames under the caller node whose id (preferred) or
session_id matches the frame's key.

This module owns the on-disk shape of those frames, the parsed-frame
cache (PERF-B: stat-keyed so re-parsing the same bytes on every reporter
tick is skipped), the recursive grafting helpers, and the tree-walk +
log-line formatters used by the live reporter.

Nothing here performs I/O outside of `_load_frames` (read + stat) and
`_most_recent_session` (project-dir scan). The reporter owns all writes.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

import yaml

from . import session
from .driver import Node, Tree


_ROOT_FRAME_KEY = "root"


# PERF-B: stat-based parsed-frame cache. Frame YAMLs are immutable once
# written by a given invocation (each fresh write produces a strictly
# newer (mtime_ns, size) tuple via atomic rename). Re-parsing the same
# bytes on every reporter tick is wasted work — yaml.safe_load is
# ~10–100× slower than dict equality. Keyed by absolute path; entries
# never expire (files in `_frames/<invoke_id>/` are removed when the
# invocation dir is cleaned up, after which subsequent stats miss).
_FRAMES_PARSED_CACHE: dict[Path, tuple[int, int, dict]] = {}
_FRAMES_PARSED_CACHE_LOCK = threading.Lock()


def _frames_cache_clear() -> None:
    """Test hook: drop the parsed-frame cache."""
    with _FRAMES_PARSED_CACHE_LOCK:
        _FRAMES_PARSED_CACHE.clear()


def _load_frames(frames_dir: Path) -> dict[str, list[dict]]:
    """Load every frame file under ``frames_dir``, grouped by ``frame_key``.

    Returns a list per key because a caller node may have issued several
    sibling nested invocations that all share its frame_key but live in
    distinct files (disambiguated by ``instance_id`` in the filename).
    Frames in each list are sorted by ``started_at`` so grafting is stable.

    Backed by a stat-keyed cache (PERF-B): re-parsing every YAML on every
    reporter tick was the second-largest cost after the merge rebuild.
    """
    out: dict[str, list[dict]] = {}
    if not frames_dir.is_dir():
        return out
    for p in frames_dir.glob("*.yaml"):
        try:
            st = p.stat()
        except OSError:
            continue
        stat_key = (st.st_mtime_ns, st.st_size)
        with _FRAMES_PARSED_CACHE_LOCK:
            cached = _FRAMES_PARSED_CACHE.get(p)
        if cached is not None and (cached[0], cached[1]) == stat_key:
            d = cached[2]
        else:
            try:
                d = yaml.safe_load(p.read_text())
            except Exception as e:
                # Parse failed (corrupt/partially-written file). Skip to
                # preserve forward progress; the producer's next atomic
                # write will land a fresh (mtime, size) tuple and we'll
                # retry. SEC-011: log so corruption is observable.
                import sys
                print(f"[callstack] ignoring malformed frame file {p}: "
                      f"{type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
                continue
            if not isinstance(d, dict):
                continue
            with _FRAMES_PARSED_CACHE_LOCK:
                _FRAMES_PARSED_CACHE[p] = (stat_key[0], stat_key[1], d)
        if not isinstance(d, dict):
            continue
        key = d.get("frame_key")
        if isinstance(key, str):
            out.setdefault(key, []).append(d)
    for key in out:
        out[key].sort(key=lambda f: str(f.get("started_at") or ""))
    return out


def _grafted_children(node_dict: dict,
                      nested_by_key: dict[str, list[dict]]) -> list[dict]:
    """Children of `node_dict` plus the root nodes of every nested frame
    whose key matches this node's id (preferred) or session_id.

    Single source of truth for the graft step shared by `_graft_node` and
    `_graft_raw` (previously had this logic duplicated verbatim)."""
    nid = str(node_dict.get("id", ""))
    sid = node_dict.get("session_id")
    children = list(node_dict.get("children") or [])
    matched = nested_by_key.get(nid) or (
        nested_by_key.get(sid) if sid else None
    ) or []
    for mf in matched:
        children.extend((mf.get("tree") or {}).get("nodes") or [])
    return children


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
    children = [
        _graft_node(c, c.get("task", ""), depth=depth + 1,
                    nested_by_session=nested_by_session)
        for c in _grafted_children(node_dict, nested_by_session)
    ]
    out: dict = {
        "id": str(node_dict.get("id", ""))[:8],
        "task": node_dict.get("task"),
        "status": _status_label_from_state(node_dict.get("state")),
        "depth": depth,
        "call_type": node_dict.get("call_type", "fork"),
        "session_id": node_dict.get("session_id"),
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


# Back-compat alias for the (kind → status) mapping; canonical source is
# in state._STATUS_BY_KIND. Re-exported here so legacy imports still work.
from .state import _STATUS_BY_KIND as _STATUS_FROM_STATE  # noqa: E402
from .state import status_label as _status_label_from_state  # noqa: E402


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
    ids from the outermost ancestor down to (but not including) this node.

    Iterative (PERF-K): explicit stack avoids per-frame Python recursion
    overhead and removes the recursion-limit risk on very deep trees.
    """
    base_chain: list[str] = list(ancestor_chain or [])
    base_depth = tree.base_depth + 1
    # Stack of (node, depth, chain_to_this_node). Push roots in reverse so
    # the first root pops first — matches the original recursive order.
    stack: list[tuple[Node, int, list[str]]] = [
        (root, base_depth, base_chain) for root in reversed(tree.nodes)
    ]
    while stack:
        node, depth, chain = stack.pop()
        yield node, depth, chain
        child_chain = chain + [node.id[:8]]
        for c in reversed(node.children):
            stack.append((c, depth + 1, child_chain))


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
    """Recursively graft nested-frame nodes under matching caller nodes,
    preserving the raw `Node.to_dict()` shape (full ids, all fields)."""
    return {**node, "children": [
        _graft_raw(c, nested) for c in _grafted_children(node, nested)
    ]}


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


_SHARED_LOCATOR: Optional["session.SessionLocator"] = None


def _most_recent_session(cwd: str) -> Optional[str]:
    """Stem of the most recently modified `.jsonl` in the cwd's project dir.

    Used to identify the calling claude session when CLAUDE_SESSION_ID is
    not exported. The active fork is the one currently being appended to,
    so it wins by mtime.

    Delegates to ``SessionLocator._most_recent`` (consolidated in PERF-F).
    Reuses one module-level locator so the per-instance MRU cache survives
    across reporter ticks and yields real benefit. The locator reads
    ``session.PROJECTS_DIR`` at construction; if a test monkeypatches
    that, recreate the shared locator to pick it up."""
    global _SHARED_LOCATOR
    if (_SHARED_LOCATOR is None
            or _SHARED_LOCATOR._projects_dir is not session.PROJECTS_DIR):
        _SHARED_LOCATOR = session.SessionLocator()
    ref = _SHARED_LOCATOR._most_recent(cwd)
    return ref.session_id if ref else None


def _one_line(s: str, limit: int) -> str:
    s = s.replace("\n", " ").replace("\r", " ").replace('"', "'")
    return s if len(s) <= limit else s[: limit - 1] + "…"
