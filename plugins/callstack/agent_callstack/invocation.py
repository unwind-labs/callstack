"""Identity + child-env propagation for one invocation.

`InvocationFactory` owns the single most subtle decision in the package:
**is this call a top-level (root) invocation, or is it nested inside an
already-running one — and how does its identity propagate to forked
children?**

A root call mints a fresh `invoke_id` and owns the whole invocation
directory. A nested call (detected when the process env carries a live
`CALLSTACK_ROOT_*` identity, stamped by a parent Driver) reuses the root's
`invoke_id` + `log_dir` so its tree merges under the caller's node in the
root's `report.yaml`.

The factory captures only a Caller's *static* configuration. It reads the
process env **lazily** — every `context()` / `parent_project_cwd()` call
re-reads it. That matters: an async host (the MCP server) may pop stale
`CALLSTACK_ROOT_*` vars between constructing a Caller and invoking it; reading
env at call time guarantees the factory agrees with that correction instead of
caching a now-wrong decision.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import env
from .env import ENV_DEPTH, ENV_MAX_DEPTH, ENV_ROOT_INVOKE_ID, ENV_ROOT_LOG_DIR
from .frames import _ROOT_FRAME_KEY
from .invocation_ctx import _InvocationContext, _new_invoke_id
from .session import most_recent_session

# ---------- the single root-vs-nested decision (pure) ----------


@dataclass(frozen=True)
class IdentityInputs:
    """Snapshot of everything the root-vs-nested decision reads, captured ONCE
    at decision time. Lazy by construction: `_capture_inputs` reads env + cwd +
    pid when `context()` / `resolve()` runs, NOT at Caller construction — so a
    host that pops stale `CALLSTACK_ROOT_*` between Caller build and invoke is
    honored. Pure data: `resolve_identity` is a function of this plus injected
    `dir_exists` / `most_recent_session`, with no env or filesystem access of
    its own (beyond those two seams)."""

    explicit_cwd: Optional[str]
    explicit_log_dir: Optional[Path]
    explicit_invoke_id: Optional[str]
    parent_cwd: Optional[str]
    root: Optional[tuple[str, str]]  # env.root_identity(): (invoke_id, log_dir) or None
    frame_key: Optional[str]  # CALLSTACK_FRAME_KEY
    claude_code_session: Optional[str]  # CLAUDE_CODE_SESSION_ID
    process_cwd: str  # os.getcwd()
    process_pid: int  # os.getpid()


@dataclass(frozen=True)
class ResolvedIdentity:
    """The single root-vs-nested decision as a value. `warning` carries a
    stale-env rejection, or an explicit-id-ignored-when-nested note, as DATA for
    the edge (the MCP boundary) to print — never stderr-from-the-core, never a
    silent drop."""

    context: _InvocationContext
    warning: Optional[str] = None


def _default_dir_exists(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False


def resolve_identity(
    inp: IdentityInputs,
    *,
    dir_exists: Callable[[Path], bool] = _default_dir_exists,
    most_recent_session: Callable[[str], Optional[str]] = most_recent_session,
) -> ResolvedIdentity:
    """THE root-vs-nested decision, made once, returned as a value.

    Nested iff a *complete* root identity is present in env AND its invocation
    dir exists (the stale-env validation that used to live in the MCP boundary,
    coordinated with the run via an `os.environ.pop`). Moving the dir check into
    the single decision means the boundary and the run derive the SAME result
    deterministically — so the env mutation is gone (DRY-102 removed).

    Id minting (`_new_invoke_id` for a fresh root, `uuid` for a nested
    `instance_id`) happens here and is the only nondeterminism; tests assert
    `is_nested` / `log_dir` / `warning`, not literal ids. The boundary mints the
    fresh id once and threads it into the run as `explicit_invoke_id`, so both
    sides agree on it."""
    effective_cwd = inp.explicit_cwd or inp.parent_cwd or inp.process_cwd
    if inp.root is not None:
        root_id, root_log = inp.root
        if dir_exists(Path(root_log) / root_id):
            key = (
                inp.frame_key
                or inp.claude_code_session
                or most_recent_session(effective_cwd)
                or f"pid-{inp.process_pid}"
            )
            warning = None
            if inp.explicit_invoke_id is not None and inp.explicit_invoke_id != root_id:
                warning = f"explicit invoke_id {inp.explicit_invoke_id!r} ignored: nested under live root {root_id!r}"
            return ResolvedIdentity(
                _InvocationContext(
                    invoke_id=root_id,
                    log_dir=Path(root_log),
                    cwd=effective_cwd,
                    frame_key=key,
                    is_nested=True,
                    # Unique per nested invocation so sibling invokes from the
                    # same caller don't share — and overwrite — one frame file.
                    instance_id=uuid.uuid4().hex[:12],
                ),
                warning,
            )
        stale_warning = (
            f"ignoring inherited {ENV_ROOT_INVOKE_ID}={root_id!r} / "
            f"{ENV_ROOT_LOG_DIR}={root_log!r} — invocation dir "
            f"{Path(root_log) / root_id} does not exist; minting a fresh invoke_id"
        )
    else:
        stale_warning = None
    return ResolvedIdentity(
        _InvocationContext(
            invoke_id=inp.explicit_invoke_id or _new_invoke_id(),
            log_dir=inp.explicit_log_dir or (Path(effective_cwd) / ".claude" / "callstack" / "log"),
            cwd=effective_cwd,
            frame_key=_ROOT_FRAME_KEY,
            is_nested=False,
        ),
        stale_warning,
    )


def _safe_getcwd() -> str:
    try:
        return os.getcwd()
    except OSError:
        return ""


def _capture_inputs(
    *,
    explicit_cwd: Optional[str],
    explicit_log_dir: Optional[Path],
    explicit_invoke_id: Optional[str],
    parent_cwd: Optional[str],
) -> IdentityInputs:
    """Read env + cwd + pid ONCE, here, at decision time (lazy contract). The
    only impure step; `resolve_identity` consumes the frozen snapshot."""
    return IdentityInputs(
        explicit_cwd=explicit_cwd,
        explicit_log_dir=explicit_log_dir,
        explicit_invoke_id=explicit_invoke_id,
        parent_cwd=parent_cwd,
        root=env.root_identity(),
        frame_key=env.frame_key(),
        claude_code_session=env.claude_code_session(),
        process_cwd=_safe_getcwd(),
        process_pid=os.getpid(),
    )


def identity_for_boundary(cwd: str) -> ResolvedIdentity:
    """The MCP boundary's single decision: resolve identity for a top-level
    `call`/`resume` with no pre-minted id. The boundary reads `ctx.invoke_id` /
    `ctx.log_dir` / `ctx.report_path` off the result, prints `warning`, and
    threads `ctx.invoke_id` into the Caller so the run reuses this exact
    decision (no second decision, no env mutation)."""
    return resolve_identity(
        _capture_inputs(
            explicit_cwd=cwd or None,
            explicit_log_dir=None,
            explicit_invoke_id=None,
            parent_cwd=None,
        )
    )


@dataclass(frozen=True)
class InvocationFactory:
    """Resolves where one Caller's invocation writes its artifacts and how its
    identity propagates to spawned children. Pure config + lazy env reads."""

    explicit_cwd: Optional[str]
    explicit_log_dir: Optional[Path]
    explicit_invoke_id: Optional[str]
    max_depth: int

    # ---- cwd / log_dir resolution ----

    def parent_project_cwd(self) -> Optional[str]:
        """Project folder of the *caller's* session — needed for cross-project
        fresh calls where `explicit_cwd` is the child's target, not the
        parent's project. Falls back through env, explicit cwd, then
        `os.getcwd()`."""
        # We're inside a nested invocation iff the parent stamped a *complete*
        # root identity into our env. Use the same predicate as `context()`
        # below — `root_identity()` requires BOTH ENV_ROOT_INVOKE_ID and
        # ENV_ROOT_LOG_DIR — so a partial env (one var set) is treated as root
        # by both methods rather than nested here and root there (L1). The
        # legacy ENV_PARENT_SESSION env var was removed (its inherited value
        # caused the regression where grandchildren forked from root instead
        # of their immediate parent).
        if env.root_identity() is not None:
            try:
                # The MCP server's os.getcwd() is reliably the caller's
                # project folder; trust it over explicit_cwd (which may be a
                # redirected child target in cross-project fresh calls).
                return os.getcwd()
            except OSError:
                pass
        return self.explicit_cwd or os.getcwd()

    def effective_cwd(self, parent_cwd: Optional[str]) -> str:
        return self.explicit_cwd or parent_cwd or os.getcwd()

    # The default log-dir layout ({cwd}/.claude/callstack/log) now lives in the
    # single `resolve_identity` decision; no separate effective_log_dir method.

    # ---- identity (nested vs root) ----

    def resolve(self, parent_cwd: Optional[str]) -> ResolvedIdentity:
        """Make the root-vs-nested decision once, returning it as a value
        (context + any warning). Snapshots env/cwd/pid HERE (lazy contract) and
        delegates to the pure `resolve_identity`."""
        return resolve_identity(
            _capture_inputs(
                explicit_cwd=self.explicit_cwd,
                explicit_log_dir=self.explicit_log_dir,
                explicit_invoke_id=self.explicit_invoke_id,
                parent_cwd=parent_cwd,
            )
        )

    def context(self, parent_cwd: Optional[str]) -> _InvocationContext:
        """Decide whether this call is a top-level (root) invocation or nested
        inside an already-running one, returning just the context. Nested calls
        inherit the root's `invoke_id` + `log_dir` from env so their tree merges
        under the caller's node in the root's report. (Callers that want the
        warning — e.g. explicit-id-ignored-when-nested — use `resolve()`.)"""
        return self.resolve(parent_cwd).context

    # ---- child env propagation ----

    def child_env(self, ctx: _InvocationContext, *, depth_base: int) -> dict[str, str]:
        """The env every spawned child claude inherits. Children read the depth
        so nested CALLs respect max_depth; the root identity propagates so a
        nested MCP invoke can find and merge into this same report.

        We deliberately do NOT propagate CALLSTACK_PARENT_SESSION (legacy env
        removed) — its inherited value caused grandchildren to resolve the
        *root* as their parent. The child's own session UUID is instead
        stamped by `ClaudeChannel._spawn()` as CALLSTACK_OWN_SESSION, paired
        with `--session-id <uuid>` on the spawned claude's argv, so it's
        deterministic and immune to env inheritance across spawn depth."""
        return {
            ENV_DEPTH: str(depth_base + 1),
            ENV_ROOT_INVOKE_ID: ctx.invoke_id,
            ENV_ROOT_LOG_DIR: str(ctx.log_dir),
            # CORR-101: stamp the effective max_depth onto every spawned child
            # so a grandchild doesn't silently revert to the default cap when
            # the root explicitly chose a smaller one. Without this,
            # ENV_MAX_DEPTH is only honored if the caller (or a human's shell)
            # happened to export it — a per-Caller max_depth=3 would be ignored
            # by claude subprocesses that inherit only the unset env, and
            # ENV_DEPTH alone would compare against the child's default.
            ENV_MAX_DEPTH: str(self.max_depth),
        }
