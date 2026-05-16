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
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

import yaml

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


_DEFAULT_REPORT_DEBOUNCE_SECS = 0.25


def _report_debounce_secs() -> float:
    """How long _LiveReporter waits before flushing a merged report write.

    Coalesces bursty Driver._notify() calls so the heavy YAML emit + atomic
    rewrite happens at most ~1/INTERVAL Hz. Tests can override via env.
    """
    raw = os.environ.get("CALLSTACK_REPORT_DEBOUNCE_SECS")
    if raw is None:
        return _DEFAULT_REPORT_DEBOUNCE_SECS
    try:
        v = float(raw)
        return v if v >= 0 else _DEFAULT_REPORT_DEBOUNCE_SECS
    except ValueError:
        return _DEFAULT_REPORT_DEBOUNCE_SECS


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
        # SEC-009: never unlink the lock file. Re-creating it between
        # acquirers races with the unlink and hands different inodes to
        # concurrent waiters, breaking mutual exclusion. Orphan lock
        # files are harmless and reused on the next run.

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

    def _do_merge(self, *, force: bool, ended_at: str) -> None:
        """Rebuild the merged report and atomically write it, skipping the
        write when the document is identical to what we last wrote (and
        ``force`` is False). Must be called with ``self._thread_lock``."""
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
            payload = yaml.safe_dump(
                doc, sort_keys=False, default_flow_style=False,
                width=120, allow_unicode=True,
            ).encode("utf-8")
            new_hash = hashlib.sha256(payload).digest()
            if not force and new_hash == self._last_merged_hash:
                return
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
