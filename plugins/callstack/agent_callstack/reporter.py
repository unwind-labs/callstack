"""Live merged-report writer for an invocation.

`_LiveReporter` is `Driver.on_progress` — fires on every state-machine
transition. It writes per-frame snapshots immediately (cheap, one
file), appends transition lines to a tail-friendly progress log
immediately (so `tail -f progress.log` stays responsive), and coalesces
the heavy merged-report rewrite behind a debounce timer (PERF-A) plus a
content-hash skip (don't rewrite if the rebuilt document is identical
to what's already on disk).

A cross-process `fcntl.flock` serializes the merge so parent and nested
writers can't corrupt each other; an in-process lock serializes parallel
roots in the same `call_many`.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

import yaml

from . import shutdown as _shutdown
from . import state as _state
from .driver import Tree
from .frames import (
    _ROOT_FRAME_KEY,
    _build_merged_report,
    _chain_to_session,
    _format_log_line,
    _load_frames,
    _merge_raw_nodes,
    _walk_tree,
)
from .invocation_ctx import _InvocationContext, _utc_now_iso


# Re-exports for backwards-compatible test access; the parsing policy
# lives in `agent_callstack.env`.
from .env import _DEFAULT_REPORT_DEBOUNCE_SECS, report_debounce_secs as _report_debounce_secs  # noqa: E402, F401


class _LiveReporter:
    """`Driver.on_progress` callback — writes per-frame snapshots immediately,
    appends transitions to a shared tail-friendly log immediately, and
    coalesces merged-report rewrites behind a debounce timer (PERF-A).

    Each invocation writes its own frame (`_frames/{key}.yaml`) containing
    its Tree. The merged `report.yaml` is rebuilt by scanning every frame
    and grafting non-root frames under their caller node. That rebuild is
    O(total nodes) per call and YAML emit dominates, so we coalesce it
    behind a ~0.25 s timer; finalize() forces a synchronous flush so the
    on-disk report is fully up to date when the run ends.

    A cross-process `fcntl.flock` serializes the merge so parent and
    nested writers can't corrupt each other's updates; an in-process lock
    serializes parallel roots in the same `call_many`."""

    def __init__(self, *, ctx: _InvocationContext, kind: str,
                 tasks: Sequence[str], started_at: str):
        self._ctx = ctx
        self._kind = kind
        self._tasks = list(tasks)
        self._started_at = started_at
        self._prev_status: dict[str, str] = {}
        self._thread_lock = threading.Lock()
        self._merge_timer: Optional[threading.Timer] = None
        self._latest_ended_at: Optional[str] = None
        # SHA-256 of the last merged-report payload we actually wrote. Used
        # to skip the atomic write when the rebuilt document is identical
        # to what's already on disk (common when notifies arrive during a
        # quiet period where only `ended_at` would change).
        self._last_merged_hash: Optional[bytes] = None
        self._finalized = False
        self._debounce = _report_debounce_secs()
        # Most recent Tree handed to __call__. Captured so the
        # shutdown-hardening path (atexit / SIGTERM / SIGINT) can write a
        # post-mortem frame for any reporter that the normal finalize()
        # never reached — without it the signal handler would have
        # nothing to serialize.
        self._latest_tree: Optional[Tree] = None
        _shutdown.register_reporter(self)

    def __call__(self, tree: Tree) -> None:
        ended_at = _utc_now_iso()
        with self._thread_lock:
            if self._finalized:
                # Late notify after finalize — driver may still emit one
                # while the timer is being cancelled; ignore.
                return
            self._ctx.invocation_dir.mkdir(parents=True, exist_ok=True)
            self._ctx.frames_dir.mkdir(parents=True, exist_ok=True)
            # Per-frame writes stay synchronous: cheap, and the merged-
            # report consumer (nested writer's next tick, unwind UI)
            # needs them visible promptly.
            self._write_frame(tree, ended_at=ended_at)
            self._latest_ended_at = ended_at
            self._latest_tree = tree
            # Schedule the merge if one isn't already pending. The first
            # action inside `_debounced_merge` is to null out the timer
            # pointer under the same lock, so the next __call__ can
            # schedule a fresh one.
            if self._merge_timer is None and self._debounce > 0:
                self._merge_timer = threading.Timer(
                    self._debounce, self._debounced_merge,
                )
                self._merge_timer.daemon = True
                self._merge_timer.start()
            elif self._debounce == 0:
                # 0-debounce: synchronous merge. Used by tests that want
                # the legacy "write report.yaml on every notify" behavior.
                self._do_merge(force=False, ended_at=ended_at)
            # Transitions log stays synchronous so `tail -f progress.log`
            # remains responsive.
            self._append_transitions(tree, ended_at)

    def _debounced_merge(self) -> None:
        with self._thread_lock:
            # finalize() may have run while the Timer thread was waiting
            # for the lock — its synchronous merge already produced the
            # authoritative report; nothing for us to do.
            self._merge_timer = None
            if self._finalized:
                return
            if self._latest_ended_at is None:
                return
            self._do_merge(force=False, ended_at=self._latest_ended_at)

    def finalize(self, tree: Tree) -> None:
        """Last write after the driver returns, so `ended_at` reflects real end.

        Cancels any pending debounced merge and runs the merge synchronously
        with force=True so the on-disk report reflects the final state even
        when the merged-document hash hasn't changed (e.g. only `ended_at`
        advanced in the last tick)."""
        ended_at = _utc_now_iso()
        with self._thread_lock:
            if self._merge_timer is not None:
                self._merge_timer.cancel()
                self._merge_timer = None
            self._finalized = True
            self._ctx.invocation_dir.mkdir(parents=True, exist_ok=True)
            self._ctx.frames_dir.mkdir(parents=True, exist_ok=True)
            self._write_frame(tree, ended_at=ended_at)
            self._latest_ended_at = ended_at
            self._latest_tree = tree
            self._do_merge(force=True, ended_at=ended_at)
            # If we're a nested invocation and the root frame still isn't on
            # disk by finalize time (root never ticked again, or root crashed
            # before its first tick), `_do_merge` silently skipped. Write a
            # `report.partial.yaml` so post-mortem consumers have *something*
            # to read about this nested call's tree — `report.yaml` is
            # left untouched (it's the root's responsibility to write).
            if self._ctx.is_nested:
                self._write_partial_if_no_root(tree, ended_at=ended_at)
            self._append_transitions(tree, ended_at)
        # Drop from the shutdown registry now that we've written the
        # authoritative final state — no need for the atexit / signal
        # handler to revisit this reporter.
        _shutdown.unregister_reporter(self)
        # SEC-009: never unlink the lock file. Re-creating it between
        # acquirers races with the unlink and hands different inodes to
        # concurrent waiters, breaking mutual exclusion. Orphan lock
        # files are harmless and reused on the next run.

    # ---- per-frame snapshot ----

    def _write_frame(self, tree: Tree, *, ended_at: str) -> None:
        # writer_pid lets the merge layer (frames._reconcile_orphan_states)
        # detect frames whose owning writer died mid-flight and promote any
        # non-terminal node states to "abandoned" — otherwise a killed/
        # crashed driver leaves "awaiting_*" states pinned forever, which
        # unwind renders as a spinning in-progress dot.
        frame = {
            "frame_key": self._ctx.frame_key,
            "is_nested": self._ctx.is_nested,
            "kind": self._kind,
            "tasks": self._tasks,
            "cwd": self._ctx.cwd,
            "writer_pid": os.getpid(),
            "started_at": self._started_at,
            "ended_at": ended_at,
            "tree": tree.to_dict(),
        }
        _atomic_yaml_write(self._ctx.frame_path(), frame)

    # ---- merged report ----

    def _do_merge(self, *, force: bool, ended_at: str) -> None:
        """Rebuild the merged report and atomically write it, skipping the
        write when the document is identical to what we last wrote (and
        ``force`` is False). Must be called with ``self._thread_lock``.

        The skip-hash is computed over the doc WITHOUT ``ended_at`` —
        ``ended_at`` is recomputed from ``_utc_now_iso()`` on every tick,
        so including it would perturb the hash on every quiet tick and
        defeat the skip (PERF-101)."""
        with _interprocess_lock(self._ctx.lock_path):
            frames = _load_frames(self._ctx.frames_dir)
            root_frames = frames.get(_ROOT_FRAME_KEY)
            if not root_frames:
                # Nested wrote first and root hasn't written yet — skip;
                # the root's next progress tick will rewrite the report.
                return
            doc = _build_merged_report(
                invoke_id=self._ctx.invoke_id, frames=frames,
                root_frame=root_frames[0], ended_at=ended_at,
            )
            new_hash = _content_hash_ignoring_ended_at(doc)
            if not force and new_hash == self._last_merged_hash:
                return
            payload = yaml.safe_dump(
                doc, sort_keys=False, default_flow_style=False,
                width=120, allow_unicode=True,
            ).encode("utf-8")
            _atomic_write_bytes(self._ctx.report_path, payload)
            self._last_merged_hash = new_hash

    def _write_partial_if_no_root(self, tree: Tree, *, ended_at: str) -> None:
        """Write `report.partial.yaml` for a nested invocation when no root
        frame exists yet.

        Called only from ``finalize``, only when ``ctx.is_nested`` is true,
        and only writes if `report.yaml` is still missing. This guarantees
        the nested invocation's tree is recoverable post-mortem even when
        the root reporter never ticked again to merge it. ``report.yaml``
        itself is left to the root reporter — a stale partial would
        otherwise mask a later real merge."""
        if self._ctx.report_path.is_file():
            return
        with _interprocess_lock(self._ctx.lock_path):
            frames = _load_frames(self._ctx.frames_dir)
            if frames.get(_ROOT_FRAME_KEY):
                # A root frame landed between our root-check above and the
                # lock acquisition — let `_do_merge` handle it on the next
                # tick (or it already did).
                return
            doc = {
                "invoke_id": self._ctx.invoke_id,
                "kind": self._kind,
                "cwd": self._ctx.cwd,
                "started_at": self._started_at,
                "ended_at": ended_at,
                "status": "partial",
                "partial_reason": (
                    "root frame not on disk at nested-invocation finalize; "
                    "the root reporter never ticked again to merge this nested tree"
                ),
                "nested_frame_key": self._ctx.frame_key,
                "tree": tree.to_dict(),
            }
            payload = yaml.safe_dump(
                doc, sort_keys=False, default_flow_style=False,
                width=120, allow_unicode=True,
            ).encode("utf-8")
            partial_path = self._ctx.invocation_dir / "report.partial.yaml"
            _atomic_write_bytes(partial_path, payload)

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

    def _emergency_finalize_on_shutdown(self) -> None:
        """Best-effort post-mortem write triggered by ``atexit`` or by a
        SIGTERM/SIGINT handler. Rewrites the latest known tree's
        non-terminal nodes as :class:`state.Abandoned` and forces a frame
        write so the on-disk frame surfaces ``status="abandoned"`` rather
        than leaving nodes pinned at ``awaiting_*``.

        Contract: must never raise — the caller is in a shutdown path
        where exceptions are swallowed at best and corrupt subsequent
        siblings' shutdown at worst. Skips silently when:

          * The normal ``finalize`` already ran (``self._finalized``).
          * No tree was ever observed (``self._latest_tree is None``).
          * ``self._thread_lock`` can't be acquired briefly — assume the
            normal finalize is currently executing and will produce the
            authoritative write.
        """
        try:
            if not self._thread_lock.acquire(timeout=0.5):
                return
        except Exception:
            return
        try:
            if self._finalized:
                return
            tree = self._latest_tree
            if tree is None:
                return
            _abandon_tree_nodes_in_place(
                tree,
                reason=f"abandoned at shutdown (pid={os.getpid()})",
            )
            try:
                self._ctx.invocation_dir.mkdir(parents=True, exist_ok=True)
                self._ctx.frames_dir.mkdir(parents=True, exist_ok=True)
                self._write_frame(tree, ended_at=_utc_now_iso())
            except Exception:
                # Shutdown is already in progress; surfacing this here
                # would only confuse the operator. Frame write may fail
                # if the disk / temp dir is already torn down.
                pass
        finally:
            try:
                self._thread_lock.release()
            except RuntimeError:
                pass

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

# SEC-009: short-poll non-blocking retry budget for the merge lock. A
# wedged sibling reporter can't deadlock the tree if we time-bound here.
_LOCK_TIMEOUT_SECONDS = 30.0


@contextlib.contextmanager
def _interprocess_lock(path: Path) -> Iterator[None]:
    """Exclusive cross-process lock around the report merge.

    Uses ``fcntl.lockf`` (POSIX byte-range locks, defined on NFS) rather
    than ``flock`` so behaviour is well-defined when ``~/.claude/`` lives
    on a mounted volume. Non-blocking with exponential backoff up to
    ``_LOCK_TIMEOUT_SECONDS`` — past that we proceed without the lock
    rather than wedging the whole tree on one stuck writer.

    SEC-009: we never unlink the lock file. Re-creating the same path
    between acquirers would race with the unlink and hand different
    inodes to concurrent waiters, breaking exclusion. Orphan lock files
    are harmless (zero bytes, reused next run).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    delay = 0.005
    with open(path, "a+") as f:
        held = False
        while True:
            try:
                fcntl.lockf(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    print(
                        f"[callstack] interprocess merge lock {path} held by "
                        f"another writer for >{_LOCK_TIMEOUT_SECONDS:.0f}s; "
                        f"proceeding without lock",
                        file=sys.stderr,
                    )
                    break
                time.sleep(delay)
                delay = min(delay * 2, 0.25)
        try:
            yield
        finally:
            if held:
                try:
                    fcntl.lockf(f.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass


def _atomic_yaml_write(path: Path, doc: Any) -> None:
    """SEC-008: write to a uniquely-named tmp file via tempfile, fsync the
    contents, then rename into place. The prior fixed `.tmp` suffix
    collided when two writers (e.g. nested + root reporters, or
    `_write_invocation_report` racing the live reporter) targeted the
    same path — one could truncate the other mid-write and publish a
    partial file. fsync ensures a crash between write and replace doesn't
    leave an empty doc as the new report."""
    payload = yaml.safe_dump(
        doc, sort_keys=False, default_flow_style=False,
        width=120, allow_unicode=True,
    )
    _atomic_write_bytes(path, payload.encode("utf-8"))


def _content_hash_ignoring_ended_at(doc: dict) -> bytes:
    """SHA-256 over a JSON canonicalization of `doc` with ``ended_at``
    stripped at the top level.

    ``ended_at`` is recomputed from wall-clock on every reporter tick;
    folding it into the hash would defeat the skip-if-unchanged check.
    Hashing the canonicalized dict (sort_keys + default=str) is faster
    than dumping YAML and avoids quoting-style perturbations. PERF-101."""
    stable = {k: v for k, v in doc.items() if k != "ended_at"}
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, default=str).encode("utf-8")
    ).digest()


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Bytes-mode counterpart to `_atomic_yaml_write` for callers that
    already serialized the YAML (e.g. to hash it for skip-if-unchanged).

    Uses tempfile.NamedTemporaryFile with `dir=path.parent` so the tmp
    sits on the same filesystem as the target (required for atomic
    `os.replace`). Each call gets a unique tmp name — concurrent writers
    no longer collide on a single shared `.tmp`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


# ---------- abandonment helpers (fix #2 + #3) ----------
#
# Both the MCP-boundary guard (fix #2) and the shutdown/signal handler
# (fix #3) need to convert non-terminal node state into a synthetic
# terminal kind so the merged report stops rendering an in-progress
# spinner. The two paths differ in *what they have access to*:
#
#   - Fix #2 runs at the MCP server's tool boundary. It only has the
#     invocation's log_dir + invoke_id; the tree is gone (it lived in
#     the worker thread). So it operates on frame YAML files on disk.
#
#   - Fix #3 runs at process shutdown. The tree is still in memory on
#     each registered `_LiveReporter`. Operating on the in-memory tree
#     keeps the abandonment write consistent with whatever the reporter
#     would have written, and skips one disk round-trip.
#
# Both ultimately produce a frame whose nodes are tagged with the
# synthetic `Abandoned` terminal kind defined in state.py.


def _abandon_tree_nodes_in_place(tree: Tree, *, reason: str) -> int:
    """Walk every node in `tree`; for any whose state is non-terminal
    (and not suspended awaiting user input), replace it with
    :class:`state.Abandoned` and mirror the error onto ``node.error`` so
    the merged-report graft surfaces it.

    Returns the number of nodes mutated. Skips terminal and
    ``AwaitingUser`` nodes (the latter is parked legitimately waiting
    for a user reply — sealing it as abandoned would lose that intent)."""
    from .driver import Node as _Node

    changed = 0
    def walk(node: _Node) -> None:
        nonlocal changed
        s = node.state
        if not _state.is_terminal(s) and not _state.is_suspended(s):
            sid = getattr(s, "session_id", None) or node.session_id
            kind = getattr(s, "kind", "unknown")
            err = f"{reason} (state was {kind!r})"
            node.state = _state.Abandoned(error=err, session_id=sid)
            node.error = err
            changed += 1
        for c in node.children:
            walk(c)
    for n in tree.nodes:
        walk(n)
    return changed


def _abandon_frame_nodes_in_place(nodes: list, *, reason: str) -> int:
    """Dict-shape counterpart to :func:`_abandon_tree_nodes_in_place`.

    Operates on the raw ``Node.to_dict()`` payload loaded from a frame
    YAML — used by :func:`_finalize_own_frames` so the MCP boundary can
    fix frames without needing to round-trip them through
    `Tree.from_dict`. Returns the number of node dicts mutated.

    Does NOT touch nodes whose ``state.kind`` is terminal or
    ``awaiting_user`` (consistent with the tree-shape variant)."""
    changed = 0
    for n in nodes:
        if not isinstance(n, dict):
            continue
        state = n.get("state")
        if isinstance(state, dict):
            kind = state.get("kind")
            if (isinstance(kind, str)
                    and kind not in _state.TERMINAL
                    and kind not in _state.SUSPENDED):
                err = f"{reason} (state was {kind!r})"
                # Preserve session_id on the State payload so downstream
                # consumers reading the dict can still chase it.
                sid = state.get("session_id") or n.get("session_id")
                new_state: dict = {"kind": "abandoned", "error": err}
                if sid:
                    new_state["session_id"] = sid
                n["state"] = new_state
                if not n.get("error"):
                    n["error"] = err
                changed += 1
        children = n.get("children")
        if isinstance(children, list):
            changed += _abandon_frame_nodes_in_place(children, reason=reason)
    return changed


def _finalize_own_frames(log_dir: Path, invoke_id: str, *,
                         reason: str) -> bool:
    """Fix #2: at the MCP server's tool boundary, force-terminate any
    non-terminal nodes in frames written by **this process** before the
    `tool_result` envelope is emitted to the parent agent.

    Acquires the same interprocess merge lock the live reporter uses, so
    the rewrite is atomic with respect to concurrent reads of the
    merged report. Only touches frames whose ``writer_pid`` field equals
    ``os.getpid()`` — frames owned by other processes (e.g. a parent
    invocation's root frame, observed because we share an invocation
    directory under nested MCP) are left untouched.

    Returns True iff at least one frame was rewritten. Safe to call
    when the invocation directory does not yet exist (no-op)."""
    invocation_dir = Path(log_dir) / invoke_id
    frames_dir = invocation_dir / "_frames"
    if not frames_dir.is_dir():
        return False
    lock_path = invocation_dir / ".report.lock"
    own_pid = os.getpid()
    rewrote_any = False
    with _interprocess_lock(lock_path):
        for frame_path in sorted(frames_dir.glob("*.yaml")):
            try:
                payload = yaml.safe_load(frame_path.read_text())
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            # Only finalize frames this process owns. A nested MCP
            # invocation under a long-lived root would otherwise wipe
            # the parent's still-running awaiting_child node when its
            # own boundary fires.
            if payload.get("writer_pid") != own_pid:
                continue
            tree = payload.get("tree")
            if not isinstance(tree, dict):
                continue
            nodes = tree.get("nodes")
            if not isinstance(nodes, list):
                continue
            if _abandon_frame_nodes_in_place(nodes, reason=reason) > 0:
                _atomic_yaml_write(frame_path, payload)
                rewrote_any = True
    return rewrote_any


# ---------- shutdown hardening (fix #3) ----------
#
# The shutdown registry, atexit, and signal-handler install live in
# `agent_callstack.shutdown` so install can happen at process startup
# from the main thread (REVIEW-202). Reporters only register/unregister
# themselves here. The names below are thin re-exports kept stable for
# tests and external callers that previously reached into this module.

from .shutdown import (  # noqa: E402
    _ACTIVE_REPORTERS,
    _ACTIVE_REPORTERS_LOCK,
    _chain_signal_handler,
    flush_active_reporters as _flush_active_reporters,
    install_shutdown_hooks,
    register_reporter as _register_active_reporter,
    unregister_reporter as _unregister_active_reporter,
)
