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
from dataclasses import dataclass
from typing import Callable, Literal, Optional

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

# Cap on outstanding `run_in_background=True` invocations parked in the
# MCP server's registry awaiting `await_call`. Each entry is cheap (one
# asyncio.Task ref) but a leaking orchestrator that fires background
# calls and never reconciles them would grow unboundedly. The cap is a
# loud failure rather than a silent LRU eviction so the operator notices
# the leak.
ENV_MAX_BACKGROUND = "CALLSTACK_MAX_BACKGROUND"

# Debounce window for LiveReporter merge ticks (reporter.py).
ENV_REPORT_DEBOUNCE_SECS = "CALLSTACK_REPORT_DEBOUNCE_SECS"

# Pre-finalize wait budget: how long the runtime is willing to block in
# `wait_for_terminal_signals` waiting for non-terminal nodes to receive a
# late `op:return` / `op:yield` envelope on their child JSONL before
# sealing the report. 0 = seal immediately (legacy behavior).
ENV_FINALIZE_WAIT_SECS = "CALLSTACK_FINALIZE_WAIT_SECONDS"

# Wall-clock TTL on a frame's `writer_pid` liveness check
# (frames._reconcile_orphan_states). A frame older than this wall-clock
# age is treated as abandoned regardless of `os.kill(pid, 0)` — defense
# against macOS PID reuse, where a dead writer's pid is recycled by an
# unrelated process and the signal-0 probe falsely reports "alive."
ENV_ORPHAN_TTL_SECS = "CALLSTACK_ORPHAN_TTL_SECONDS"

# How long synchronous `call` (run_in_background=False) is willing to
# block before returning a `status: "pending"` envelope so the
# orchestrator can drain via `await_call`. Exists because the MCP
# client (Claude Code) imposes a hard wall-clock cap per tool call
# (`MCP_TOOL_TIMEOUT`, default ~10 min) that progress notifications do
# NOT extend. Set this below the client's cap with a safety margin so
# the sync path can degrade gracefully into the background-await path
# instead of being killed mid-flight. 0 disables the auto-fallback
# (sync `call` will block until the child finishes or the client
# kills it).
ENV_SYNC_BUDGET_SECS = "CALLSTACK_SYNC_BUDGET_SECONDS"


# ---------- Defaults ----------

_DEFAULT_MAX_DEPTH = 10
_DEFAULT_MAX_FANOUT = 64
_DEFAULT_MAX_BACKGROUND = 64
_DEFAULT_REPORT_DEBOUNCE_SECS = 0.25
_DEFAULT_FINALIZE_WAIT_SECS = 120.0
_MAX_FINALIZE_WAIT_SECS = 600.0
# 2 × Claude Code's default MCP tool timeout (~10 min) — past this point
# we declare a writer dead regardless of what `os.kill(pid, 0)` says.
_DEFAULT_ORPHAN_TTL_SECS = 1200.0
_MAX_ORPHAN_TTL_SECS = 24 * 60 * 60.0

# 540s = 9 min — one minute under Claude Code's default 10 min
# MCP_TOOL_TIMEOUT so the sync await unwinds cleanly into a
# `status: "pending"` envelope before the client kills the tool call.
# Operators with a custom MCP_TOOL_TIMEOUT should tune this to stay
# under it. Clamp at 24h: there is no legitimate reason for a sync
# await to exceed a day, and an absurdly large value almost certainly
# indicates a config typo we'd rather expose than honor.
_DEFAULT_SYNC_BUDGET_SECS = 540.0
_MAX_SYNC_BUDGET_SECS = 24 * 60 * 60.0

# SEC-103: defensive ceiling on the depth budget. A caller (or stale
# shell env) setting `CALLSTACK_MAX_DEPTH=1_000_000` would let a runaway
# tree fork itself into oblivion. 32 is far above any legitimate
# workflow and still leaves the host healthy.
_MAX_DEPTH_CEILING = 32


# ---------- Numeric readers (single clamp/default policy) ----------
#
# Every numeric knob shares one shape: read env → unset?default → parse
# (ValueError?default) → reject out-of-range?default → clamp to ceiling.
# Declaring each as data and running it through `_read_numeric` keeps that
# policy single-sourced (a bug fixed once is fixed for every knob) and makes
# adding a knob a one-line table entry. The public reader names + int/float
# return types and each knob's exact default/ceiling/reject-policy are
# unchanged; the rich rationale for each lives on its constants above.


@dataclass(frozen=True)
class _NumericKnob:
    name: str
    parse: Callable[[str], float]  # `int` or `float`
    default: float
    # max_depth/fanout/background reject `<= 0` (a count must be positive);
    # the duration knobs reject only `< 0` (0 is a meaningful "disable").
    reject: Literal["<=0", "<0"]
    ceiling: float | None = None  # `None` = no upper clamp


def _read_numeric(k: _NumericKnob) -> float:
    raw = os.environ.get(k.name)
    if raw is None:
        return k.default
    try:
        v = k.parse(raw)
    except ValueError:
        return k.default
    if (k.reject == "<=0" and v <= 0) or (k.reject == "<0" and v < 0):
        return k.default
    return min(v, k.ceiling) if k.ceiling is not None else v


_MAX_DEPTH_KNOB = _NumericKnob(ENV_MAX_DEPTH, int, _DEFAULT_MAX_DEPTH, "<=0", _MAX_DEPTH_CEILING)
_MAX_FANOUT_KNOB = _NumericKnob(ENV_MAX_FANOUT, int, _DEFAULT_MAX_FANOUT, "<=0")
_MAX_BACKGROUND_KNOB = _NumericKnob(ENV_MAX_BACKGROUND, int, _DEFAULT_MAX_BACKGROUND, "<=0")
_REPORT_DEBOUNCE_KNOB = _NumericKnob(ENV_REPORT_DEBOUNCE_SECS, float, _DEFAULT_REPORT_DEBOUNCE_SECS, "<0")
_FINALIZE_WAIT_KNOB = _NumericKnob(
    ENV_FINALIZE_WAIT_SECS, float, _DEFAULT_FINALIZE_WAIT_SECS, "<0", _MAX_FINALIZE_WAIT_SECS
)
_ORPHAN_TTL_KNOB = _NumericKnob(ENV_ORPHAN_TTL_SECS, float, _DEFAULT_ORPHAN_TTL_SECS, "<0", _MAX_ORPHAN_TTL_SECS)
_SYNC_BUDGET_KNOB = _NumericKnob(ENV_SYNC_BUDGET_SECS, float, _DEFAULT_SYNC_BUDGET_SECS, "<0", _MAX_SYNC_BUDGET_SECS)


def max_depth() -> int:
    """Effective max recursion depth, clamped to `_MAX_DEPTH_CEILING`."""
    return int(_read_numeric(_MAX_DEPTH_KNOB))


def max_fanout() -> int:
    """Max `len(tasks)` accepted at the MCP boundary."""
    return int(_read_numeric(_MAX_FANOUT_KNOB))


def max_background() -> int:
    """Max number of `run_in_background=True` invocations the MCP server
    will keep parked in its registry at once."""
    return int(_read_numeric(_MAX_BACKGROUND_KNOB))


def report_debounce_secs() -> float:
    """How long _LiveReporter waits before flushing a merged report.
    Tests override via env to 0 for synchronous merge."""
    return _read_numeric(_REPORT_DEBOUNCE_KNOB)


def read_finalize_wait_seconds() -> float:
    """How long `wait_for_terminal_signals` will block on non-terminal nodes
    before marking them `Timeout`. Clamped to `[0, _MAX_FINALIZE_WAIT_SECS]`.
    Setting `CALLSTACK_FINALIZE_WAIT_SECONDS=0` preserves the pre-fix
    "seal immediately" behavior."""
    return _read_numeric(_FINALIZE_WAIT_KNOB)


def read_orphan_ttl_seconds() -> float:
    """Wall-clock age past which a frame's `writer_pid` is considered
    abandoned regardless of `os.kill(pid, 0)`. Belt-and-suspenders against
    PID reuse on macOS / busy hosts. Clamped to `[0, _MAX_ORPHAN_TTL_SECS]`.

    Setting `CALLSTACK_ORPHAN_TTL_SECONDS=0` opts out of the TTL fallback
    entirely (relies on `_pid_alive` alone — restores the pre-fix
    behavior for tests that want to pin it)."""
    return _read_numeric(_ORPHAN_TTL_KNOB)


def sync_budget_secs() -> float:
    """How long synchronous `call` will block awaiting the child before
    returning a `status: "pending"` envelope. Returns 0 to disable the
    auto-fallback. Clamped to `[0, _MAX_SYNC_BUDGET_SECS]`."""
    return _read_numeric(_SYNC_BUDGET_KNOB)


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
