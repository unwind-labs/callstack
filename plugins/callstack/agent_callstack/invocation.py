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
from typing import Optional

from . import env
from .env import ENV_DEPTH, ENV_MAX_DEPTH, ENV_ROOT_INVOKE_ID, ENV_ROOT_LOG_DIR
from .frames import _ROOT_FRAME_KEY
from .invocation_ctx import _InvocationContext, _new_invoke_id
from .session import most_recent_session


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
        # We're inside a nested invocation iff the parent stamped its root
        # identity into our env. ENV_ROOT_INVOKE_ID is the canonical "we're
        # nested" signal; the legacy ENV_PARENT_SESSION env var was removed
        # (its inherited value caused the regression where grandchildren
        # forked from root instead of their immediate parent).
        if env.in_nested_invocation():
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

    def effective_log_dir(self, cwd: str) -> Path:
        return self.explicit_log_dir or (Path(cwd) / ".claude" / "callstack" / "log")

    # ---- identity (nested vs root) ----

    def context(self, parent_cwd: Optional[str]) -> _InvocationContext:
        """Decide whether this call is a top-level (root) invocation or nested
        inside an already-running one. Nested calls inherit the root's
        `invoke_id` + `log_dir` from env so their tree can be merged under the
        caller's node in the root's report."""
        effective_cwd = self.effective_cwd(parent_cwd)
        root = env.root_identity()
        if root is not None:
            root_id, root_log = root
            # Deterministic: the parent Driver stamped this subprocess with
            # the caller node's id via CALLSTACK_FRAME_KEY. Fall back to
            # session heuristics only if the env didn't survive (shouldn't
            # happen with a current agent-callstack parent, but keeps us
            # robust when nested under an older runtime).
            key = (
                env.frame_key()
                or env.claude_code_session()
                or most_recent_session(effective_cwd)
                or f"pid-{os.getpid()}"
            )
            return _InvocationContext(
                invoke_id=root_id,
                log_dir=Path(root_log),
                cwd=effective_cwd,
                frame_key=key,
                is_nested=True,
                # Unique per nested invocation so multiple sibling invokes
                # from the same caller (e.g. a deep-rewrite fork running
                # specialists, then meta-assessors, then re-author) don't
                # share — and overwrite — one frame file.
                instance_id=uuid.uuid4().hex[:12],
            )
        return _InvocationContext(
            invoke_id=self.explicit_invoke_id or _new_invoke_id(),
            log_dir=self.effective_log_dir(effective_cwd),
            cwd=effective_cwd,
            frame_key=_ROOT_FRAME_KEY,
            is_nested=False,
        )

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
