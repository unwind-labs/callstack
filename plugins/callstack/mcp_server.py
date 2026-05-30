"""MCP server: thin shim over agent_callstack.

Exposes `call` and `resume` as MCP tools by calling the runtime in-process.
The previous version shelled out to callstack.py via subprocess.exec — that
round-trip is gone.

`call` always takes `tasks: list[str]` (single or multiple) and always
returns `{invoke_id, report_path, results: [...]}`. `resume` continues a
previously yielded session.

Each call generates an `invoke_id` and writes:
  - `{cwd}/.claude/callstack/log/{invoke_id}/call_trace.jsonl`  per-turn trace
  - `{cwd}/.claude/callstack/log/{invoke_id}.yaml`              tree report
The response envelope includes `invoke_id` and `report_path` so the caller
can open the YAML and see the full nested call tree with inputs/outputs.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from agent_callstack import (
    ENV_ROOT_INVOKE_ID,
    ENV_ROOT_LOG_DIR,
    BackgroundRuns,
    Caller,
    CallFailed,
    CallYielded,
    CapReached,
    Crashed,
    Done,
    MultiResult,
    NotFound,
    Pending,
    Result,
    SessionLocator,
    Started,
    YieldToken,
    max_fanout,
    new_invoke_id,
    root_identity,
    sync_budget_secs,
)
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("call")


# Registry of in-flight `call` invocations launched with run_in_background=True.
# `BackgroundRuns` owns the full lifecycle (scheduling, cap, reap, await,
# crash-finalize). Lives at module scope because FastMCP runs every tool call
# on the same event loop, so a task scheduled inside `call` keeps running while
# subsequent tool calls (including `await_call`) are served from the same
# process.
_background = BackgroundRuns()


def _result_to_dict(item: Any) -> dict:
    """Translate a Result / CallYielded / CallFailed into the wire envelope."""
    if isinstance(item, (Result, CallYielded, CallFailed)):
        return item.to_envelope()
    raise TypeError(f"unknown result type: {type(item).__name__}")


# SEC-102: defense-in-depth against a hostile or buggy MCP client
# submitting unbounded task lists. Each task forks a `claude`
# subprocess (RSS 0.5–2 GB), so even one MCP call with a 10k-element
# array can OOM the host. The cap can be widened via env for users
# with legitimate batch needs, but never silently exceeded.


def _max_fanout() -> int:
    """Thin wrapper kept for backwards-compatible test access; the
    parsing policy lives in `agent_callstack.max_fanout`."""
    return max_fanout()


def _validate_tasks(tasks: Any) -> Optional[str]:
    """Return an error message if `tasks` is unusable, else None.

    Rejects: non-list, empty list, list over the fanout cap, list
    containing non-string or empty entries. All checks are at the MCP
    boundary so they apply uniformly to every client."""
    if not isinstance(tasks, list):
        return f"tasks must be a list, got {type(tasks).__name__}"
    if not tasks:
        return "tasks list is empty; pass at least one task"
    cap = _max_fanout()
    if len(tasks) > cap:
        return (
            f"tasks list has {len(tasks)} entries; max fanout is {cap}. "
            f"Set CALLSTACK_MAX_FANOUT in env to widen the cap."
        )
    for i, t in enumerate(tasks):
        if not isinstance(t, str):
            return f"tasks[{i}] must be a string, got {type(t).__name__}"
        if not t.strip():
            return f"tasks[{i}] is empty or whitespace-only"
    return None


def _log_dir(cwd: str) -> Path:
    return Path(cwd or os.getcwd()) / ".claude" / "callstack" / "log"


def _report_path(log_dir: Path, invoke_id: str) -> str:
    return str(log_dir / invoke_id / "report.yaml")


def _resolve_invocation_identity(cwd: str) -> tuple[str, Path]:
    """Resolve the (invoke_id, log_dir) this MCP call will write to.

    NOT a pure accessor: on stale-env rejection this MUTATES os.environ
    (pops CALLSTACK_ROOT_*) as a normalization side effect — see DRY-102
    below. Named `_resolve_*` (like `_resolve_cwd`) to signal that it
    actively computes-and-normalizes rather than just reads.

    If the process env carries `CALLSTACK_ROOT_*` it means we're running
    inside an already-live invocation (nested MCP call) — reuse that root's
    identity so our work merges into its report.yaml rather than spawning
    a fresh top-level invocation. Otherwise mint a new id.

    Validation: an env-supplied root must point at a real, writable invocation
    directory. A stale shell that leaked these vars from a prior run would
    otherwise silently misroute the new top-level call into a nonexistent
    or unrelated dir. On validation failure we fall through to minting a
    fresh id and log a warning to stderr."""
    root = root_identity()
    if root is not None:
        root_id, root_dir = root
        root_dir_path = Path(root_dir)
        invocation_dir = root_dir_path / root_id
        # The invocation subdir is created at root start-up by the reporter;
        # its presence proves these env vars came from a live parent rather
        # than a stale shell export.
        if root_dir_path.is_dir() and invocation_dir.is_dir():
            return root_id, root_dir_path
        print(
            f"[callstack] WARN: ignoring inherited "
            f"{ENV_ROOT_INVOKE_ID}={root_id!r} / "
            f"{ENV_ROOT_LOG_DIR}={root_dir!r} — invocation dir "
            f"{invocation_dir!s} does not exist; minting a fresh invoke_id",
            file=sys.stderr,
        )
        # DRY-102: clear the stale env so the downstream Caller's
        # `_resolve_invocation_context` (which reads env directly)
        # agrees with our decision. Otherwise Caller would treat the
        # invocation as nested under the dead root and try to write
        # frames into a nonexistent dir.
        os.environ.pop(ENV_ROOT_INVOKE_ID, None)
        os.environ.pop(ENV_ROOT_LOG_DIR, None)
    return new_invoke_id(), _log_dir(cwd)


def _build_caller(session: str, model: str, cwd: str, timeout: int, invoke_id: str, log_dir: Path) -> Caller:
    return Caller(
        session=session or None,
        model=model or None,
        cwd=cwd or None,
        timeout=timeout,
        invoke_id=invoke_id,
        log_dir=log_dir,
    )


def _parent_project_folder() -> str:
    """The caller's project folder. The MCP server runs with cwd == caller's
    project folder, so os.getcwd() is reliably the parent's project."""
    return os.getcwd()


def _sensitive_prefixes() -> list[Path]:
    """System / user-secret locations that callstack refuses to use as cwd
    unless the parent project itself happens to live under one of them."""
    home = Path.home()
    return [
        Path("/etc"),
        Path("/var"),
        Path("/usr"),
        Path("/bin"),
        Path("/sbin"),
        Path("/private/etc"),
        Path("/private/var"),
        home / ".ssh",
        home / ".aws",
        home / ".gnupg",
        home / ".config",
    ]


def _resolve_cwd(raw: str) -> tuple[str, Optional[str]]:
    """Expand `{PWD}` against the caller's project folder, then canonicalize.

    Returns (resolved_abs_path, error_message). On success error_message is
    None and resolved_abs_path is an absolute directory that exists. On
    failure resolved_abs_path may be a partial expansion (for error display)
    and error_message describes what went wrong.

    Empty `raw` resolves to the caller's project folder (the default behavior
    before this feature) — no validation needed in that case.

    Canonicalizes symlinks via `Path.resolve(strict=True)` and rejects paths
    that resolve under sensitive system/user prefixes (e.g. /etc, ~/.ssh)
    unless they fall under the parent project folder itself."""
    if not raw:
        return _parent_project_folder(), None
    expanded = raw.replace("{PWD}", _parent_project_folder())
    expanded = os.path.abspath(os.path.expanduser(expanded))
    try:
        resolved = Path(expanded).resolve(strict=True)
    except FileNotFoundError:
        return expanded, f"cwd '{expanded}' is not an existing directory"
    except OSError as e:
        return expanded, f"cwd '{expanded}' could not be resolved: {e}"
    if not resolved.is_dir():
        return str(resolved), f"cwd '{resolved}' is not an existing directory"
    parent_project = Path(_parent_project_folder()).resolve()
    if resolved != parent_project and not _is_within(resolved, parent_project):
        for prefix in _sensitive_prefixes():
            try:
                prefix_resolved = prefix.resolve(strict=True)
            except (FileNotFoundError, OSError):
                continue
            # If the parent project itself lives under this sensitive prefix
            # (e.g. macOS tmp at /private/var/folders/...), the user has
            # already accepted that location as their workspace — don't
            # gate siblings at the same level.
            if parent_project == prefix_resolved or _is_within(parent_project, prefix_resolved):
                continue
            if resolved == prefix_resolved or _is_within(resolved, prefix_resolved):
                return str(resolved), (
                    f"cwd '{resolved}' is in a sensitive system/user "
                    f"location ('{prefix}') and is not allowed; pass an "
                    f"explicit project subdirectory"
                )
    return str(resolved), None


def _is_within(child: Path, parent: Path) -> bool:
    """True iff `child` lies inside `parent` (both already resolved)."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _same_project(a: str, b: str) -> bool:
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return False


def _envelope_from_multi(invoke_id: str, report_path: str, multi: MultiResult) -> dict:
    envelope: dict = {
        "invoke_id": invoke_id,
        "report_path": report_path,
        "results": [_result_to_dict(r) for r in multi.results],
    }
    # The reporter swallows write failures internally; verify here so the
    # parent agent knows the `report_path` it just received is unreliable
    # rather than silently consuming a stale or missing file.
    if not Path(report_path).is_file():
        envelope["report_warning"] = (
            f"report file at {report_path!r} does not exist on disk; the live reporter may have failed to write it"
        )
    return envelope


@mcp.tool()
async def call(
    tasks: list[str],
    timeout: int = 300,
    session_id: str = "",
    model: str = "",
    cwd: str = "",
    context: str = "fork",
    run_in_background: bool = False,
) -> str:
    """Fork sub-agents to execute `tasks` concurrently. Returns an envelope
    `{invoke_id, report_path, results: [...]}`; all tasks share one
    invocation and one YAML report listing the full nested call tree.

    Always pass an array — one task or many. Tasks must be independent when
    more than one is supplied.

    context — how each task's underlying claude session is launched:
        "fork"  (default) — child inherits the parent's full conversation
                            context via `--resume + --fork-session`. Best
                            for "delegate a follow-up step the child
                            already understands."
        "fresh"           — brand-new isolated session. Only the task
                            string crosses the boundary (same semantics as
                            Claude Code's built-in Agent / Task tool).
                            Use when you want an independent worker that
                            shouldn't see the parent transcript.

    cwd — project folder to run the child in. Supports `{PWD}` substitution
        against the caller's project folder, e.g. `"{PWD}/../sibling-repo"`
        to run the child in an adjacent checkout. Cross-project use is
        ONLY supported with context="fresh"; combining context="fork"
        with a different project folder returns an error envelope.

    run_in_background — when True, the tool returns immediately with
        `{invoke_id, report_path, status: "started"}` once input validation
        passes; the actual fan-out runs in a background asyncio.Task.
        Use `await_call(invoke_id)` to reconcile the final envelope.
        Designed for batches whose wallclock would otherwise exceed
        Claude Code's `MCP_TOOL_TIMEOUT` (~10 min). Validation errors
        (bad tasks, bad cwd, fork+cross-project) are still surfaced
        synchronously so the orchestrator can react without polling.

    Synchronous-mode auto-fallback: when `run_in_background=False`, the
        sync await is bounded by `CALLSTACK_SYNC_BUDGET_SECONDS` (default
        540s, one minute under Claude Code's default 10 min MCP cap). A
        run that finishes inside the budget returns the full `results`
        envelope as before. A run that's still going when the budget
        elapses returns `{invoke_id, report_path, status: "pending"}` —
        identical to what `await_call` returns mid-run — so the
        orchestrator can drain it with `await_call(invoke_id)` instead
        of being killed by the MCP client. Set the env var to 0 to
        disable the fallback (block until done or until the client
        kills the tool call). Calls have no inherent timeout — the
        budget exists only to dodge the MCP client's hard cap."""
    if context not in ("fork", "fresh"):
        return json.dumps(
            {
                "invoke_id": "",
                "report_path": "",
                "results": [{"status": "error", "error": f"invalid context: {context!r} (must be 'fork' or 'fresh')"}],
            },
            indent=2,
        )
    tasks_err = _validate_tasks(tasks)
    if tasks_err:
        return json.dumps(
            {
                "invoke_id": "",
                "report_path": "",
                "results": [{"status": "error", "error": tasks_err}],
            },
            indent=2,
        )
    resolved_cwd, cwd_err = _resolve_cwd(cwd)
    if cwd_err:
        return json.dumps(
            {
                "invoke_id": "",
                "report_path": "",
                "results": [{"status": "error", "error": cwd_err}],
            },
            indent=2,
        )
    parent_dir = _parent_project_folder()
    if context == "fork" and not _same_project(resolved_cwd, parent_dir):
        return json.dumps(
            {
                "invoke_id": "",
                "report_path": "",
                "results": [
                    {
                        "status": "error",
                        "error": (
                            f"context='fork' cannot be combined with a cwd "
                            f"different from the parent's project folder "
                            f"(parent={parent_dir}, requested={resolved_cwd}); "
                            f"use context='fresh' instead"
                        ),
                    }
                ],
            },
            indent=2,
        )

    invoke_id, log_dir = _resolve_invocation_identity(resolved_cwd)
    caller = _build_caller(session_id, model, resolved_cwd, timeout, invoke_id, log_dir)
    report_path = _report_path(log_dir, invoke_id)

    if run_in_background:
        # `BackgroundRuns.start` reaps finished-unreconciled runs, enforces the
        # cap, and schedules `caller.call_many` on a worker thread — returning
        # a typed outcome we translate to the wire envelope here.
        outcome = _background.start(
            invoke_id=invoke_id,
            caller=caller,
            tasks=tasks,
            context=context,
            report_path=report_path,
            log_dir=log_dir,
        )
        if isinstance(outcome, CapReached):
            return json.dumps(
                {
                    "invoke_id": "",
                    "report_path": "",
                    "results": [
                        {
                            "status": "error",
                            "error": (
                                f"background-call registry full: {outcome.outstanding} "
                                f"outstanding (cap={outcome.cap}). Reconcile pending "
                                f"invocations with `await_call(invoke_id)` first, or "
                                f"widen with CALLSTACK_MAX_BACKGROUND."
                            ),
                        }
                    ],
                },
                indent=2,
            )
        assert isinstance(outcome, Started)
        return json.dumps(
            {
                "invoke_id": outcome.invoke_id,
                "report_path": outcome.report_path,
                "status": "started",
            },
            indent=2,
        )

    # Sync path: route through the same `BackgroundRuns` machinery as
    # `run_in_background=True`, but bound-await the result so quick
    # tasks still return their full envelope inline. The unification
    # buys two things over the previous `asyncio.to_thread` direct
    # path:
    #   1. If the await elapses without completion, we return a
    #      `status: "pending"` envelope and leave the task parked in
    #      the registry, so the orchestrator can `await_call` it to
    #      drain — sidestepping Claude Code's `MCP_TOOL_TIMEOUT` hard
    #      cap (which progress notifications do NOT extend; see env.py
    #      `ENV_SYNC_BUDGET_SECS`).
    #   2. `BackgroundRuns.reconcile` already owns the crash-finalize
    #      path (`_finalize_crashed`), replacing the bespoke
    #      `_finalize_at_boundary` guard that previously wrapped
    #      `call_many`.
    start_outcome = _background.start(
        invoke_id=invoke_id,
        caller=caller,
        tasks=tasks,
        context=context,
        report_path=report_path,
        log_dir=log_dir,
    )
    if isinstance(start_outcome, CapReached):
        return json.dumps(
            {
                "invoke_id": "",
                "report_path": "",
                "results": [
                    {
                        "status": "error",
                        "error": (
                            f"background-call registry full: {start_outcome.outstanding} "
                            f"outstanding (cap={start_outcome.cap}). Reconcile pending "
                            f"invocations with `await_call(invoke_id)` first, or "
                            f"widen with CALLSTACK_MAX_BACKGROUND."
                        ),
                    }
                ],
            },
            indent=2,
        )
    assert isinstance(start_outcome, Started)
    outcome = await _background.reconcile(
        invoke_id,
        timeout=sync_budget_secs(),
    )
    if isinstance(outcome, Pending):
        # Budget elapsed before the run finished. The entry stays
        # pinned in the registry (`_Run.polled=True`); orchestrator
        # drains it with `await_call(invoke_id)`. Same wire shape as
        # `await_call`'s pending response so callers have one drain
        # code path.
        return json.dumps(
            {
                "invoke_id": invoke_id,
                "report_path": outcome.report_path,
                "status": "pending",
            },
            indent=2,
        )
    if isinstance(outcome, Crashed):
        # `BackgroundRuns` already force-finalized the crashed run's
        # frames; surface the structured error envelope rather than
        # re-raising so the orchestrator sees the same shape it would
        # for a background-mode crash.
        return json.dumps(
            {
                "invoke_id": invoke_id,
                "report_path": outcome.report_path,
                "results": [
                    {
                        "status": "error",
                        "error": f"call_many raised: {outcome.error}",
                    }
                ],
            },
            indent=2,
        )
    assert isinstance(outcome, Done)
    return json.dumps(
        _envelope_from_multi(invoke_id, outcome.report_path, outcome.result),
        indent=2,
    )


@mcp.tool()
async def await_call(invoke_id: str, timeout: int = 60) -> str:
    """Reconcile a `call` started with run_in_background=True.

    Returns the same `{invoke_id, report_path, results: [...]}` envelope as
    a synchronous `call` once the background task finishes. If the task is
    still running after `timeout` seconds, returns
    `{invoke_id, report_path, status: "pending"}` and leaves the task in
    the registry — call `await_call` again to keep waiting. Unknown
    invoke_ids return a status='error' envelope.

    A successful (or errored) reconciliation removes the entry from the
    registry; pending reconciliations leave it in place."""
    # `BackgroundRuns.reconcile` owns the shield-on-await, the keep-on-pending
    # semantics, and force-finalizing a crashed run's frames. We just map its
    # typed outcome onto the wire envelope.
    outcome = await _background.reconcile(invoke_id, timeout=timeout)
    if isinstance(outcome, NotFound):
        return json.dumps(
            {
                "invoke_id": invoke_id,
                "status": "error",
                "error": f"no background call with invoke_id={invoke_id!r}",
            },
            indent=2,
        )
    if isinstance(outcome, Pending):
        return json.dumps(
            {
                "invoke_id": invoke_id,
                "report_path": outcome.report_path,
                "status": "pending",
            },
            indent=2,
        )
    if isinstance(outcome, Crashed):
        return json.dumps(
            {
                "invoke_id": invoke_id,
                "report_path": outcome.report_path,
                "status": "error",
                "error": f"background call raised: {outcome.error}",
            },
            indent=2,
        )
    assert isinstance(outcome, Done)
    return json.dumps(
        _envelope_from_multi(invoke_id, outcome.report_path, outcome.result),
        indent=2,
    )


@mcp.tool()
async def resume(resume_session: str, user_reply: str, timeout: int = 300, cwd: str = "") -> str:
    """Resume a previously yielded call session with the user's reply.

    Use after a call returned status 'yield' — pass back the session_id and the
    user's answer. The clone_path comes from the same yield envelope."""
    # M-A2: apply the same cwd hardening call() does — {PWD} expansion,
    # symlink canonicalization, existence/dir check, sensitive-prefix gating.
    # Without this, resume() honored a raw {PWD} token literally and would
    # happily run inside /etc or ~/.ssh, a divergent contract for the same
    # conceptual parameter. We do NOT replicate call()'s fork+cross-project
    # block: resume has no `context` param and legitimately continues a
    # `fresh` cross-project yielded session, so cross-project cwd is allowed
    # here (the sensitive-prefix gate still applies).
    resolved_cwd, cwd_err = _resolve_cwd(cwd)
    if cwd_err:
        return json.dumps(
            {
                "invoke_id": "",
                "report_path": "",
                "status": "error",
                "error": cwd_err,
            }
        )
    invoke_id, log_dir = _resolve_invocation_identity(resolved_cwd)
    caller = _build_caller("", "", resolved_cwd, timeout, invoke_id, log_dir)
    # Locate the clone path so we can construct a YieldToken. Pass None when
    # the caller gave no explicit cwd: SessionLocator.resolve only does the
    # cross-project full scan when cwd is None, and a resumed `fresh`
    # cross-project session may live outside the parent project. An explicit
    # cwd stays caller-scoped, now canonicalized to match call()'s contract.
    locate_cwd = resolved_cwd if cwd else None
    clone = SessionLocator().resolve(resume_session, cwd=locate_cwd)
    envelope = {"invoke_id": invoke_id, "report_path": _report_path(log_dir, invoke_id)}
    if clone is None:
        envelope.update({"status": "error", "error": f"Cannot find session file for {resume_session}"})
        return json.dumps(envelope)
    token = YieldToken(session_id=resume_session, clone_path=str(clone))
    try:
        result = await asyncio.to_thread(caller.resume, token, user_reply)
        envelope.update(_result_to_dict(result))
    except CallYielded as y:
        envelope.update(_result_to_dict(y))
    except CallFailed as f:
        envelope.update(_result_to_dict(f))
    return json.dumps(envelope)


if __name__ == "__main__":
    mcp.run()
