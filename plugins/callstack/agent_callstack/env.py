"""Single source of truth for callstack-runtime env vars.

Every `CALLSTACK_*` variable name lives here, plus the third-party
`CLAUDE_CODE_SESSION_ID` we read from Claude Code. Consumers import the
constants directly when they only need the key (e.g. when stamping env
onto a subprocess), and the typed readers when they want the parsed value
plus default + fallback policy in one call.

Previously these names were scattered across `__init__.py`,
`session.py`, `channel.py`, `reporter.py`, `mcp_server.py`, and
`frames.py` — some as `ENV_*` constants, some as `_ENV_*` private
constants, some as bare string literals inside ``os.environ.get()``.
DRY-101 consolidates them so a future rename / addition is one edit
not five.
"""
from __future__ import annotations

import os
from typing import Optional


# ---------- Variable names ----------

# Depth counter: parent stamps `current+1` onto every spawned child so a
# deeply nested chain can hit `max_depth()` from any process. Inherited
# transparently across `claude --fork-session` spawns.
ENV_DEPTH = "CALLSTACK_DEPTH"

# Identity of the top-level (root) invocation. Stamped onto every
# spawned child so a nested MCP call can recognize it's inside an
# active invocation and merge its tree into the root's `report.yaml`
# rather than starting a fresh top-level run.
ENV_ROOT_INVOKE_ID = "CALLSTACK_ROOT_INVOKE_ID"
ENV_ROOT_LOG_DIR = "CALLSTACK_ROOT_LOG_DIR"

# Per-spawn frame identity: parent Driver stamps the spawned node's id
# here. A nested MCP invoke inside that subprocess reads it back to
# identify its frame deterministically (no session-id heuristics).
ENV_FRAME_KEY = "CALLSTACK_FRAME_KEY"

# UUID stamped by `ClaudeChannel._spawn()` into the child's env,
# paired with `--session-id <uuid>` on the child's claude argv. The
# child's MCP server reads it back to identify its own session id
# without relying on Claude Code's env propagation behavior across a
# `--fork-session` spawn (which is opaque/unspecified for stdio MCP
# children of a forked subprocess).
ENV_OWN_SESSION = "CALLSTACK_OWN_SESSION"

# Claude Code's own per-process session id. Fallback when
# `ENV_OWN_SESSION` is absent (the user's top-level interactive
# `claude` session, which we didn't spawn).
ENV_CLAUDE_SESSION = "CLAUDE_CODE_SESSION_ID"

# Caller-overridable max recursion depth. Stamped onto every spawned
# child so grandchildren inherit the budget the root chose (CORR-101).
ENV_MAX_DEPTH = "CALLSTACK_MAX_DEPTH"

# MCP-boundary fanout cap (SEC-102). Each task in the array forks a
# `claude` subprocess at 0.5–2 GB RSS, so unbounded `len(tasks)` is a
# trivial DoS. Widenable for legitimate batch needs.
ENV_MAX_FANOUT = "CALLSTACK_MAX_FANOUT"

# Process-pool sizing (channel.py).
ENV_MAX_CONCURRENT_FORKS = "CALLSTACK_MAX_CONCURRENT_FORKS"
ENV_MAX_IN_FLIGHT_TURNS = "CALLSTACK_MAX_IN_FLIGHT_TURNS"

# Debounce window for LiveReporter merge ticks (reporter.py).
ENV_REPORT_DEBOUNCE_SECS = "CALLSTACK_REPORT_DEBOUNCE_SECS"


# ---------- Defaults ----------

_DEFAULT_MAX_DEPTH = 10
_DEFAULT_MAX_FANOUT = 64
_DEFAULT_MAX_CONCURRENT_FORKS = 8
_DEFAULT_REPORT_DEBOUNCE_SECS = 0.25

# SEC-103: defensive ceiling on the depth budget. A caller (or stale
# shell env) setting `CALLSTACK_MAX_DEPTH=1_000_000` would let a runaway
# tree fork itself into oblivion. 32 is far above any legitimate
# workflow and still leaves the host healthy.
_MAX_DEPTH_CEILING = 32


# ---------- Typed readers ----------

def max_depth() -> int:
    """Effective max recursion depth, clamped to `_MAX_DEPTH_CEILING`."""
    raw = os.environ.get(ENV_MAX_DEPTH)
    if raw is None:
        return _DEFAULT_MAX_DEPTH
    try:
        v = int(raw)
        if v <= 0:
            return _DEFAULT_MAX_DEPTH
        return min(v, _MAX_DEPTH_CEILING)
    except ValueError:
        return _DEFAULT_MAX_DEPTH


def max_fanout() -> int:
    """Max `len(tasks)` accepted at the MCP boundary."""
    raw = os.environ.get(ENV_MAX_FANOUT)
    if raw is None:
        return _DEFAULT_MAX_FANOUT
    try:
        v = int(raw)
        return v if v > 0 else _DEFAULT_MAX_FANOUT
    except ValueError:
        return _DEFAULT_MAX_FANOUT


def max_concurrent_forks() -> int:
    raw = os.environ.get(ENV_MAX_CONCURRENT_FORKS, str(_DEFAULT_MAX_CONCURRENT_FORKS))
    try:
        v = int(raw)
        return v if v > 0 else _DEFAULT_MAX_CONCURRENT_FORKS
    except ValueError:
        return _DEFAULT_MAX_CONCURRENT_FORKS


def max_in_flight_turns() -> int:
    raw = os.environ.get(ENV_MAX_IN_FLIGHT_TURNS,
                         str(max_concurrent_forks() * 2))
    try:
        v = int(raw)
        return v if v > 0 else max_concurrent_forks() * 2
    except ValueError:
        return max_concurrent_forks() * 2


def report_debounce_secs() -> float:
    """How long _LiveReporter waits before flushing a merged report.
    Tests override via env to 0 for synchronous merge."""
    raw = os.environ.get(ENV_REPORT_DEBOUNCE_SECS)
    if raw is None:
        return _DEFAULT_REPORT_DEBOUNCE_SECS
    try:
        v = float(raw)
        return v if v >= 0 else _DEFAULT_REPORT_DEBOUNCE_SECS
    except ValueError:
        return _DEFAULT_REPORT_DEBOUNCE_SECS


def current_depth() -> int:
    """Read the depth counter the parent stamped into our env (0 at root)."""
    try:
        return int(os.environ.get(ENV_DEPTH, "0"))
    except ValueError:
        return 0


def root_identity() -> Optional[tuple[str, str]]:
    """Returns `(invoke_id, log_dir)` if env carries a live root identity,
    else None. Just reads — does NOT validate that the log_dir exists;
    callers that need that should validate themselves (mcp_server does)."""
    invoke_id = os.environ.get(ENV_ROOT_INVOKE_ID)
    log_dir = os.environ.get(ENV_ROOT_LOG_DIR)
    if invoke_id and log_dir:
        return invoke_id, log_dir
    return None


def frame_key() -> Optional[str]:
    return os.environ.get(ENV_FRAME_KEY)


def own_session() -> Optional[str]:
    return os.environ.get(ENV_OWN_SESSION)


def claude_code_session() -> Optional[str]:
    return os.environ.get(ENV_CLAUDE_SESSION)


def in_nested_invocation() -> bool:
    """True iff the env claims we're inside an already-running invocation."""
    return os.environ.get(ENV_ROOT_INVOKE_ID) is not None
