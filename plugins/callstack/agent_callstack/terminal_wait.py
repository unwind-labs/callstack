"""Pre-finalize wait for late terminal envelopes.

When `Driver.run` returns, some nodes may still be in `AwaitingTurn` —
typically because the child claude session was mid-permission-prompt or
otherwise stalled while emitting its `op:return` envelope, and the
synchronous channel read in `Driver._run_turn` had already collected its
`result` line. In that case the envelope arrives on the child's session
JSONL *after* the parent finalize would have sealed the report.

This module is the safety net: before `reporter.finalize(tree)` runs,
`wait_for_terminal_signals` tails each non-terminal node's clone_path
JSONL looking for the matching fenced ```json envelope and feeds it
through the canonical `state.step` transition. Nodes that don't produce
one within `wait_budget_seconds` are transitioned to `state.Timeout` so
the merged report records `status: "timeout"` explicitly instead of
silently mislabeling them `running`.

Reuses `session.envelope_from_session_record` (record-shape decoding) and
`state.step` (the transition machine) — this module owns only the polling
loop and the recovered-duration arithmetic.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from . import sealing, state as st
from .driver import Node, Tree
from .protocol import Return, Yield
from .session import envelope_from_session_record, session_record_epoch

# Polling cadence for the wait loop. Small enough that a 1s late envelope
# is observed promptly; large enough that the loop is cheap on the
# common "nothing to wait for" path.
_POLL_INTERVAL_SECS = 0.25


def wait_for_terminal_signals(
    tree: Tree,
    *,
    wait_budget_seconds: float = 120.0,
) -> None:
    """Block up to `wait_budget_seconds` waiting for terminal envelopes to
    land on the child JSONL of each non-terminal node in `tree`. Applies
    any recovered state in-place. Remaining non-terminal nodes at budget
    end transition to `state.Timeout`.

    Never raises. Never kills any process (per design: an orphan pool
    entry can finish on its own and is reaped at shutdown).
    """
    if wait_budget_seconds <= 0:
        # Legacy "seal immediately" behavior — finalize handles whatever
        # state the tree is in. Tests use this to assert pre-fix shape.
        return

    waiters = [_NodeWaiter(node) for node in _all_nodes(tree) if st.is_eligible_for_abandonment(node.state.kind)]
    if not waiters:
        return

    deadline = time.monotonic() + wait_budget_seconds
    while waiters and time.monotonic() < deadline:
        waiters = [w for w in waiters if not w.poll()]
        if not waiters:
            return
        time.sleep(_POLL_INTERVAL_SECS)

    # Budget elapsed: anything still non-terminal becomes Timeout.
    for w in waiters:
        w.expire_to_timeout()


# ---------- internals ----------


def _all_nodes(tree: Tree):
    """Pre-order traversal of every node in `tree`."""
    stack: list[Node] = list(tree.nodes)
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)


def _session_id_of(state: st.State) -> Optional[str]:
    sid = getattr(state, "session_id", None)
    return sid if isinstance(sid, str) and sid else None


class _NodeWaiter:
    """Per-node state for the polling loop.

    Tails `node.clone_path` line by line, decoding each line as JSON and
    feeding assistant rows to `envelope_from_session_record`. The first
    matching `op:return` / `op:yield` is applied via `state.step`, which
    resolves the waiter.
    """

    __slots__ = ("node", "_file_offset", "_session_start_epoch")

    def __init__(self, node: Node):
        self.node = node
        self._file_offset = 0
        # Wall-clock start of the child session, read from the first JSONL
        # record's timestamp. Forked sessions replay the parent transcript
        # (old timestamps) *after* a fork-creation header carrying the
        # recent timestamp, so the first record in file order is the
        # session's true start — `min(timestamps)` would wrongly pick a
        # replayed parent record. Used to recompute duration on recovery.
        self._session_start_epoch: Optional[float] = None

    def poll(self) -> bool:
        """Read any new JSONL lines and try to apply a terminal envelope.

        Returns True iff the waiter is resolved (envelope applied). False
        means "keep polling" — the budget-expiry path seals it as Timeout.
        """
        clone_path = self.node.clone_path
        if not clone_path:
            # No JSONL was ever resolved for this node; the only honest
            # signal is the wait budget. Keep polling so expiry catches it.
            return False
        path = Path(clone_path)
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size <= self._file_offset:
            return False

        envelope, end_epoch = self._read_new(path, size)
        if envelope is None:
            return False
        return self._apply(envelope, end_epoch)

    def expire_to_timeout(self) -> None:
        """Force the node to `Timeout` — called once the global wait
        budget runs out without a recoverable envelope. Routes the Timeout
        construction through the shared sealing policy so the wait-budget
        message and session-id handling stay single-sourced."""
        sid = _session_id_of(self.node.state) or self.node.session_id
        self.node.state = sealing.terminal_state_for(
            sealing.TimeoutCause(),
            prior_kind=self.node.state.kind,
            session_id=sid,
        )

    # ---- envelope reading ----

    def _read_new(self, path: Path, size: int):
        """Read bytes from `self._file_offset` up to the last complete line,
        capture the session-start timestamp, and return the first
        `(envelope, end_epoch)` found — or `(None, None)`."""
        try:
            with path.open("rb") as fh:
                fh.seek(self._file_offset)
                chunk = fh.read(size - self._file_offset)
        except OSError:
            return None, None
        # Only commit complete lines; a partial trailing line is re-read on
        # the next poll once the writer finishes it.
        last_newline = chunk.rfind(b"\n")
        if last_newline < 0:
            return None, None
        self._file_offset += last_newline + 1
        text = chunk[: last_newline + 1].decode("utf-8", errors="replace")

        for line in text.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if self._session_start_epoch is None:
                self._session_start_epoch = session_record_epoch(record)
            envelope = envelope_from_session_record(record)
            if envelope is not None:
                return envelope, session_record_epoch(record)
        return None, None

    def _apply(self, envelope, end_epoch: Optional[float]) -> bool:
        """Apply a recovered Return / Yield to the node via `state.step`.

        Only `AwaitingTurn` can absorb a `TurnCompleted`; an `AwaitingChild`
        or `Pending` node has no in-flight turn whose envelope this could
        be, so we leave it for the budget-expiry Timeout path. CALL
        envelopes can't be recovered post-hoc either — the would-be child
        never spawned — so they're ignored too.
        """
        node = self.node
        if not isinstance(node.state, st.AwaitingTurn):
            return False
        if not isinstance(envelope, (Return, Yield)):
            return False
        sid = _session_id_of(node.state) or node.session_id or "unknown"
        new_state, _ = st.step(
            node.state,
            st.TurnCompleted(envelope=envelope, session_id=sid),
        )
        node.state = new_state
        # Recompute duration from the JSONL's own timestamps rather than
        # the finalize-wait latency: a 200s task that lands its envelope
        # 0.2s into the wait must record ~200s, not 0.2s.
        if isinstance(new_state, st.Done) and self._session_start_epoch is not None and end_epoch is not None:
            node.duration = max(0.0, end_epoch - self._session_start_epoch)
        return True
