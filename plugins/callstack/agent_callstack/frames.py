"""Frame loading + merge helpers for the invocation report.

Each invocation writes a YAML frame file to `_frames/{key}.yaml` carrying
its Tree. The merged `report.yaml` is built by scanning every frame and
grafting non-root frames under the caller node whose id (preferred) or
session_id matches the frame's key.

This module owns the on-disk shape of those frames, the parsed-frame
cache (PERF-B: stat-keyed so re-parsing the same bytes on every reporter
tick is skipped), the recursive grafting helpers, and the tree-walk +
log-line formatters used by the live reporter.

Nothing here performs I/O outside of `_load_frames` (read + stat). The
reporter owns all writes; the mtime-based "which session is the caller"
lookup lives in `session.most_recent_session`.
"""

from __future__ import annotations

import copy
import os
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from .driver import Node, Tree
from .env import read_orphan_ttl_seconds

_ROOT_FRAME_KEY = "root"


# PERF-B: stat-based parsed-frame cache. Frame YAMLs are immutable once
# written by a given invocation (each fresh write produces a strictly
# newer (mtime_ns, size) tuple via atomic rename). Re-parsing the same
# bytes on every reporter tick is wasted work — yaml.safe_load is
# ~10–100× slower than dict equality.
#
# PERF-103: bounded LRU. Long-lived MCP server processes were growing
# unboundedly because frames from each new nested invocation pile up.
# OrderedDict gives O(1) move-to-end / popitem(last=False).
_FRAMES_PARSED_CACHE_MAX = 2048
_FRAMES_PARSED_CACHE: "OrderedDict[Path, tuple[int, int, dict]]" = OrderedDict()
_FRAMES_PARSED_CACHE_LOCK = threading.Lock()

# PERF-104: dir-mtime fast-path. The reporter calls `_load_frames` on
# every tick; for a typical invocation the frames_dir has a few files
# and the directory mtime only advances when a new frame lands. When
# nothing has changed we can skip the glob + per-file stat entirely
# and reuse the last aggregated result.
_FRAMES_DIR_CACHE_MAX = 64
_FRAMES_DIR_CACHE: "OrderedDict[Path, tuple[int, dict]]" = OrderedDict()
_FRAMES_DIR_CACHE_LOCK = threading.Lock()

# SEC-006: safety bounds on the frames-dir scan. A stray or hostile
# process that drops files into a known frames_dir can otherwise stall
# the merge loop.
_MAX_FRAMES_PER_LOAD = 2048
_MAX_FRAME_FILE_BYTES = 2 * 1024 * 1024  # 2 MiB
_FRAME_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _frames_cache_clear() -> None:
    """Test hook: drop both parsed-frame and dir-mtime caches."""
    with _FRAMES_PARSED_CACHE_LOCK:
        _FRAMES_PARSED_CACHE.clear()
    with _FRAMES_DIR_CACHE_LOCK:
        _FRAMES_DIR_CACHE.clear()


def _cache_put_parsed(path: Path, value: tuple[int, int, dict]) -> None:
    """LRU put for `_FRAMES_PARSED_CACHE`. Caller holds the lock."""
    _FRAMES_PARSED_CACHE[path] = value
    _FRAMES_PARSED_CACHE.move_to_end(path)
    while len(_FRAMES_PARSED_CACHE) > _FRAMES_PARSED_CACHE_MAX:
        _FRAMES_PARSED_CACHE.popitem(last=False)


def _cache_put_dir(path: Path, value: tuple[int, dict]) -> None:
    """LRU put for `_FRAMES_DIR_CACHE`. Caller holds the lock."""
    _FRAMES_DIR_CACHE[path] = value
    _FRAMES_DIR_CACHE.move_to_end(path)
    while len(_FRAMES_DIR_CACHE) > _FRAMES_DIR_CACHE_MAX:
        _FRAMES_DIR_CACHE.popitem(last=False)


def _pid_alive(pid: int) -> bool:
    """True if signal 0 reaches `pid`. False on ESRCH (process gone) or
    invalid pid; True on EPERM (process exists but we lack permission).

    Only a liveness probe — does not verify process identity. A reused PID
    is treated as "alive," which is the safe direction (we'd skip
    reconciliation rather than falsely mark a live invocation as
    abandoned)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _frame_age_seconds(frame: dict, *, now: Optional[float] = None) -> Optional[float]:
    """Wall-clock age of a frame in seconds, derived from its
    ``started_at`` ISO-8601 field. Returns None when the field is missing
    or unparseable — callers must treat that as "age unknown" rather than
    "definitely young." Tolerates the trailing ``Z`` shorthand the rest
    of the codebase emits via :func:`invocation_ctx._utc_now_iso`."""
    started_at = frame.get("started_at")
    if not isinstance(started_at, str) or not started_at:
        return None
    iso = started_at.rstrip()
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Treat naive timestamps as UTC — the producer (`_utc_now_iso`)
        # always emits a timezone, so this only fires for externally-
        # produced frames; choosing UTC is the safe default.
        dt = dt.replace(tzinfo=timezone.utc)
    wall_now = time.time() if now is None else now
    return wall_now - dt.timestamp()


def _frame_writer_is_dead(frame: dict, *, ttl_seconds: float, now: Optional[float] = None) -> bool:
    """True iff the frame's `writer_pid` is no longer alive OR its
    wall-clock age exceeds ``ttl_seconds``.

    The TTL fallback is the defense against PID reuse: macOS recycles
    PIDs within a few thousand spawns, so a writer that died and had its
    PID reclaimed by an unrelated process would otherwise stay
    "alive-looking" forever. Setting ``ttl_seconds=0`` opts out of the
    TTL check entirely (PID liveness alone)."""
    pid = frame.get("writer_pid")
    if not isinstance(pid, int):
        return False
    if not _pid_alive(pid):
        return True
    if ttl_seconds <= 0:
        return False
    age = _frame_age_seconds(frame, now=now)
    if age is None:
        return False
    return age > ttl_seconds


def _reconcile_orphan_states(frames_by_key: dict[str, list[dict]]) -> None:
    """Walk every frame; if its `writer_pid` field names a process that is
    no longer alive (or the frame is older than the orphan TTL, defense
    against PID reuse), promote any non-terminal node state inside that
    frame to the synthetic terminal kind ``"abandoned"``.

    Mutates the frame dicts in place. Idempotent — once a frame is
    reconciled, subsequent passes are no-ops (dead pids stay dead;
    "abandoned" kinds are already terminal).

    Frames with no `writer_pid` field (older runs, externally-produced
    frames) are skipped: we can't decide liveness, so we leave the frame
    alone."""
    ttl_seconds = read_orphan_ttl_seconds()
    now = time.time()
    for frames in frames_by_key.values():
        for frame in frames:
            if not _frame_writer_is_dead(frame, ttl_seconds=ttl_seconds, now=now):
                continue
            pid = frame.get("writer_pid")
            assert isinstance(pid, int)  # _frame_writer_is_dead enforces this
            tree = frame.get("tree")
            if not isinstance(tree, dict):
                continue
            nodes = tree.get("nodes")
            if not isinstance(nodes, list):
                continue
            mark_abandoned_in_dict_nodes(
                nodes,
                reason=f"writer pid {pid} is no longer alive",
            )


def mark_abandoned_in_dict_nodes(nodes: list, *, reason: str) -> int:
    """Seal every eligible non-terminal node in a frame-YAML (dict) node list
    to the ``"abandoned"`` kind, recursively, preserving session_id and
    stamping the reason + prior kind onto both `state.error` and the top-level
    `node.error` (without clobbering an existing one).

    Thin entry point over the shared `sealing.seal_tree` walk — the dict-shape
    adapter and the `state.is_eligible_for_abandonment` policy live there,
    single-sourced with the in-memory Tree variant in `reporter.py`. Returns
    the count of nodes sealed."""
    from .sealing import AbandonCause, seal_tree, tree_dict_views

    return seal_tree(tree_dict_views(nodes), AbandonCause(reason))


def _load_frames(frames_dir: Path) -> dict[str, list[dict]]:
    """Load every frame file under ``frames_dir``, grouped by ``frame_key``.

    Returns a list per key because a caller node may have issued several
    sibling nested invocations that all share its frame_key but live in
    distinct files (disambiguated by ``instance_id`` in the filename).
    Frames in each list are sorted by ``started_at`` so grafting is stable.

    Backed by a stat-keyed cache (PERF-B): re-parsing every YAML on every
    reporter tick was the second-largest cost after the merge rebuild.

    Contract — the returned structure is fully owned by the caller and
    safe to mutate (including the inner frame dicts and their nested
    ``tree``/``state`` fields). The internal caches (parsed-frame and
    dir-mtime) deep-copy on retrieval to keep that promise: orphan
    reconciliation, graft helpers, and tests can all rewrite frame
    contents without poisoning subsequent loads.
    """
    import sys

    out: dict[str, list[dict]] = {}
    if not frames_dir.is_dir():
        return out
    # PERF-104: dir-mtime fast-path. If the directory mtime hasn't
    # advanced since our last successful aggregation, no file was
    # atomically renamed in or out — return the cached result.
    try:
        dir_mtime = frames_dir.stat().st_mtime_ns
    except OSError:
        dir_mtime = None
    if dir_mtime is not None:
        with _FRAMES_DIR_CACHE_LOCK:
            cached = _FRAMES_DIR_CACHE.get(frames_dir)
            if cached is not None and cached[0] == dir_mtime:
                _FRAMES_DIR_CACHE.move_to_end(frames_dir)
                # Deep-copy so the caller can mutate frame contents freely
                # (orphan reconciliation rewrites `state.kind`; the merge
                # layer grafts children in place) without corrupting the
                # cached snapshot — restoring the documented "callers can
                # mutate freely" contract. The copy is cheap relative to
                # the YAML parse it replaces.
                snapshot = copy.deepcopy(cached[1])
                _reconcile_orphan_states(snapshot)
                return snapshot
    loaded = 0
    for p in frames_dir.glob("*.yaml"):
        if loaded >= _MAX_FRAMES_PER_LOAD:
            print(
                f"[callstack] frames_dir {frames_dir} contains more than "
                f"{_MAX_FRAMES_PER_LOAD} files; further frames ignored",
                file=sys.stderr,
            )
            break
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size > _MAX_FRAME_FILE_BYTES:
            print(
                f"[callstack] skipping oversized frame file {p} ({st.st_size} bytes > {_MAX_FRAME_FILE_BYTES})",
                file=sys.stderr,
            )
            continue
        stat_key = (st.st_mtime_ns, st.st_size)
        d: Optional[dict] = None
        with _FRAMES_PARSED_CACHE_LOCK:
            cached = _FRAMES_PARSED_CACHE.get(p)
            if cached is not None and (cached[0], cached[1]) == stat_key:
                _FRAMES_PARSED_CACHE.move_to_end(p)
                # Deep-copy so downstream mutations (orphan reconciliation,
                # graft helpers) don't bleed back into the parsed-frame
                # cache. yaml.safe_load already returns fresh dicts on a
                # parse-miss; we keep the invariant uniform.
                d = copy.deepcopy(cached[2])
        if d is None:
            try:
                parsed = yaml.safe_load(p.read_text())
            except Exception as e:
                # Parse failed (corrupt/partially-written file). Skip to
                # preserve forward progress; the producer's next atomic
                # write will land a fresh (mtime, size) tuple and we'll
                # retry. SEC-011: log so corruption is observable.
                print(
                    f"[callstack] ignoring malformed frame file {p}: {type(e).__name__}: {str(e)[:200]}",
                    file=sys.stderr,
                )
                continue
            if not isinstance(parsed, dict):
                continue
            d = parsed
            with _FRAMES_PARSED_CACHE_LOCK:
                _cache_put_parsed(p, (stat_key[0], stat_key[1], d))
        # `d` is non-None here: cache-hit set it, or the parse branch
        # set it (else `continue` exited the iteration).
        assert d is not None
        key = d.get("frame_key")
        # SEC-006: reject frames whose key doesn't match the expected
        # shape (root frame, hex uuid, or hex-uuid-instance pair).
        if not (isinstance(key, str) and _FRAME_KEY_RE.fullmatch(key)):
            continue
        out.setdefault(key, []).append(d)
        loaded += 1
    for key in out:
        out[key].sort(key=lambda f: str(f.get("started_at") or ""))
    # PERF-104: cache the aggregated result under the dir's mtime so a
    # quiet tick reuses it. Deep-copy so the cache stores a frozen
    # snapshot — the caller (and `_reconcile_orphan_states` below) will
    # mutate `out` in place; the cached copy stays pristine.
    if dir_mtime is not None:
        with _FRAMES_DIR_CACHE_LOCK:
            _cache_put_dir(frames_dir, (dir_mtime, copy.deepcopy(out)))
    # Reconciliation runs on `out`, the caller-side copy. The dir-mtime
    # cache hit path above re-runs reconciliation on each retrieval; the
    # PID check per node is cheap relative to the YAML parse we already
    # skipped, and idempotent rewrites are harmless.
    _reconcile_orphan_states(out)
    return out


def _grafted_children(node_dict: dict, nested_by_key: dict[str, list[dict]]) -> list[dict]:
    """Children of `node_dict` plus the root nodes of every nested frame
    whose key matches this node's id (preferred) or session_id.

    Single source of truth for the graft step shared by `_graft_node` and
    `_graft_raw` (previously had this logic duplicated verbatim)."""
    nid = str(node_dict.get("id", ""))
    sid = node_dict.get("session_id")
    children = list(node_dict.get("children") or [])
    matched = nested_by_key.get(nid) or (nested_by_key.get(sid) if sid else None) or []
    for mf in matched:
        children.extend((mf.get("tree") or {}).get("nodes") or [])
    return children


def _build_merged_report(*, invoke_id: str, frames: dict[str, list[dict]], root_frame: dict, ended_at: str) -> dict:
    """Produce the report.yaml document by grafting each non-root frame's
    tree under the node (anywhere in the root's tree) whose id matches the
    frame key — the caller node id (set via CALLSTACK_FRAME_KEY) preferred,
    session_id as fallback (see `_grafted_children`). Multiple frames may
    share a key (one per sibling nested invocation); their nodes are
    concatenated under the matching caller node, in started_at order."""
    root_tree = root_frame.get("tree", {})
    root_nodes = root_tree.get("nodes", []) or []
    nested_by_key = {k: v for k, v in frames.items() if k != _ROOT_FRAME_KEY}
    tasks = root_frame.get("tasks") or []
    merged_nodes = [
        _graft_node(
            n,
            tasks[i] if i < len(tasks) else n.get("task", ""),
            depth=root_tree.get("base_depth", 0) + 1,
            nested_by_key=nested_by_key,
        )
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
            sum(float(n.get("duration_seconds", 0.0)) for n in merged_nodes),
            2,
        ),
        "status": overall,
        "nested_frames": sorted(nested_by_key.keys()),
        "tasks": merged_nodes,
    }


def _graft_node(node_dict: dict, input_text: str, *, depth: int, nested_by_key: dict[str, list[dict]]) -> dict:
    """Render one Node.to_dict() into report shape, attaching nested-frame
    children whose frame key matches this node's id (preferred, set by the
    parent Driver via CALLSTACK_FRAME_KEY) or session_id (fallback). When
    multiple frames share that key (sibling nested invocations from the
    same caller), all of their nodes graft in — sorted by frame
    ``started_at`` so order is stable."""
    children = [
        _graft_node(c, c.get("task", ""), depth=depth + 1, nested_by_key=nested_by_key)
        for c in _grafted_children(node_dict, nested_by_key)
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
    if statuses == {"timeout"}:
        return "timeout"
    if statuses == {"abandoned"}:
        return "abandoned"
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
    stack: list[tuple[Node, int, list[str]]] = [(root, base_depth, base_chain) for root in reversed(tree.nodes)]
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
    return f'[{ts}] d={depth} {indent}[{id_chain}] {node.status:<9} task="{task}"{detail}'


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
    return {**node, "children": [_graft_raw(c, nested) for c in _grafted_children(node, nested)]}


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


def _one_line(s: str, limit: int) -> str:
    # SEC-106: strip ASCII control chars (incl. ANSI escape ESC=\x1b) so a
    # malicious task or LLM-controlled `result` can't smuggle terminal
    # escape sequences into `tail -f progress.log`. Newline / tab collapse
    # to space (the old behavior — friendly for one-line display);
    # everything else in 0x00–0x1F or 0x7F becomes `?`. 0x80+ stays so
    # Unicode strings pass through.
    def sanitize(c: str) -> str:
        o = ord(c)
        if c in ("\n", "\r", "\t"):
            return " "
        if o < 0x20 or o == 0x7F:
            return "?"
        return c

    s = "".join(sanitize(c) for c in s).replace('"', "'")
    return s if len(s) <= limit else s[: limit - 1] + "…"
