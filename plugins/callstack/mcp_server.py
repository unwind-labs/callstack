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
    Caller, CallFailed, CallYielded, MultiResult, Result, YieldToken,
    _new_invoke_id,
)
from agent_callstack import env as _env
from agent_callstack.reporter import _finalize_own_frames
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("call")


# Registry of in-flight `call` invocations launched with run_in_background=True.
# Keyed by invoke_id; the `await_call` tool drains entries as the orchestrator
# reconciles them. Lives at module scope because FastMCP runs every tool call
# on the same event loop, so the background asyncio.Task started inside `call`
# continues running while subsequent tool calls (including `await_call`) are
# served from the same process.
_background_tasks: dict[str, dict] = {}


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
    parsing policy lives in `agent_callstack.env.max_fanout`."""
    return _env.max_fanout()


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


def _invocation_identity(cwd: str) -> tuple[str, Path]:
    """Return the (invoke_id, log_dir) this MCP call will actually write to.

    If the process env carries `CALLSTACK_ROOT_*` it means we're running
    inside an already-live invocation (nested MCP call) — reuse that root's
    identity so our work merges into its report.yaml rather than spawning
    a fresh top-level invocation. Otherwise mint a new id.

    Validation: an env-supplied root must point at a real, writable invocation
    directory. A stale shell that leaked these vars from a prior run would
    otherwise silently misroute the new top-level call into a nonexistent
    or unrelated dir. On validation failure we fall through to minting a
    fresh id and log a warning to stderr."""
    root = _env.root_identity()
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
            f"{_env.ENV_ROOT_INVOKE_ID}={root_id!r} / "
            f"{_env.ENV_ROOT_LOG_DIR}={root_dir!r} — invocation dir "
            f"{invocation_dir!s} does not exist; minting a fresh invoke_id",
            file=sys.stderr,
        )
        # DRY-102: clear the stale env so the downstream Caller's
        # `_resolve_invocation_context` (which reads env directly)
        # agrees with our decision. Otherwise Caller would treat the
        # invocation as nested under the dead root and try to write
        # frames into a nonexistent dir.
        os.environ.pop(_env.ENV_ROOT_INVOKE_ID, None)
        os.environ.pop(_env.ENV_ROOT_LOG_DIR, None)
    return _new_invoke_id(), _log_dir(cwd)


def _build_caller(session: str, model: str, cwd: str, timeout: int,
                  invoke_id: str, log_dir: Path) -> Caller:
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
        Path("/etc"), Path("/var"), Path("/usr"), Path("/bin"), Path("/sbin"),
        Path("/private/etc"), Path("/private/var"),
        home / ".ssh", home / ".aws", home / ".gnupg", home / ".config",
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
            if (parent_project == prefix_resolved
                    or _is_within(parent_project, prefix_resolved)):
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


def _reap_finished_background_tasks() -> None:
    """Drop registry entries whose underlying task has already completed
    (success or exception) but were never reconciled via `await_call`.

    Reconciliation normally pops the entry, so anything still here when
    done() is True is leaked. We retire those silently — the task result
    is unreachable without the original invoke_id, and a leaked entry
    shouldn't burn a slot against `CALLSTACK_MAX_BACKGROUND`. Exceptions
    are surfaced to the asyncio loop via task.result() so they're not
    swallowed; we then discard them since no caller is listening."""
    stale = [iid for iid, e in _background_tasks.items() if e["task"].done()]
    for iid in stale:
        task = _background_tasks.pop(iid)["task"]
        # Consume any exception so asyncio doesn't log a
        # "Task exception was never retrieved" warning at GC.
        if not task.cancelled():
            task.exception()


def _finalize_at_boundary(log_dir, invoke_id: str, *, reason: str) -> None:
    """Best-effort post-mortem at an MCP tool exception boundary.

    Force-terminates any non-terminal frames this process owns so the
    parent agent sees status='abandoned' rather than a stuck-running
    spinner. Wraps `_finalize_own_frames` with the standard "never raise,
    log to stderr on failure" contract.

    NOT run on the happy path: `caller.call_many` already runs
    `reporter.finalize` (which itself runs `wait_for_terminal_signals`)
    in its own finally. The boundary cleanup only matters when an
    exception aborts that chain — running it on success was measurable
    I/O (glob + fcntl lock + per-frame parse) for a guaranteed no-op."""
    try:
        _finalize_own_frames(log_dir, invoke_id, reason=reason)
    except Exception as e:
        print(f"[callstack] WARN _finalize_own_frames raised at MCP "
              f"boundary ({reason}): {type(e).__name__}: {e}",
              file=sys.stderr)


def _envelope_from_multi(invoke_id: str, report_path: str,
                          multi: MultiResult) -> dict:
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
            f"report file at {report_path!r} does not exist on disk; "
            f"the live reporter may have failed to write it"
        )
    return envelope


@mcp.tool()
async def call(tasks: list[str], timeout: int = 300, session_id: str = "",
               model: str = "", cwd: str = "", context: str = "fork",
               run_in_background: bool = False) -> str:
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
        synchronously so the orchestrator can react without polling."""
    if context not in ("fork", "fresh"):
        return json.dumps({
            "invoke_id": "", "report_path": "",
            "results": [{"status": "error",
                         "error": f"invalid context: {context!r} "
                                  f"(must be 'fork' or 'fresh')"}],
        }, indent=2)
    tasks_err = _validate_tasks(tasks)
    if tasks_err:
        return json.dumps({
            "invoke_id": "", "report_path": "",
            "results": [{"status": "error", "error": tasks_err}],
        }, indent=2)
    resolved_cwd, cwd_err = _resolve_cwd(cwd)
    if cwd_err:
        return json.dumps({
            "invoke_id": "", "report_path": "",
            "results": [{"status": "error", "error": cwd_err}],
        }, indent=2)
    parent_dir = _parent_project_folder()
    if context == "fork" and not _same_project(resolved_cwd, parent_dir):
        return json.dumps({
            "invoke_id": "", "report_path": "",
            "results": [{
                "status": "error",
                "error": (
                    f"context='fork' cannot be combined with a cwd "
                    f"different from the parent's project folder "
                    f"(parent={parent_dir}, requested={resolved_cwd}); "
                    f"use context='fresh' instead"),
            }],
        }, indent=2)

    invoke_id, log_dir = _invocation_identity(resolved_cwd)
    caller = _build_caller(session_id, model, resolved_cwd, timeout,
                           invoke_id, log_dir)
    report_path = _report_path(log_dir, invoke_id)

    if run_in_background:
        cap = _env.max_background()
        # Reap any already-finished tasks the orchestrator never reconciled;
        # a slow operator shouldn't trip the cap just because they queued
        # several quick background calls and only awaited some.
        _reap_finished_background_tasks()
        if len(_background_tasks) >= cap:
            return json.dumps({
                "invoke_id": "", "report_path": "",
                "results": [{
                    "status": "error",
                    "error": (
                        f"background-call registry full: {len(_background_tasks)} "
                        f"outstanding (cap={cap}). Reconcile pending invocations "
                        f"with `await_call(invoke_id)` first, or widen with "
                        f"CALLSTACK_MAX_BACKGROUND."
                    ),
                }],
            }, indent=2)
        task = asyncio.create_task(
            asyncio.to_thread(caller.call_many, tasks, context=context)
        )
        # Stash log_dir on the registry entry so `await_call` can run the
        # atomic-emit guard against the right invocation directory
        # without having to re-derive it from `report_path`.
        _background_tasks[invoke_id] = {
            "task": task, "report_path": report_path, "log_dir": log_dir,
        }
        return json.dumps({
            "invoke_id": invoke_id,
            "report_path": report_path,
            "status": "started",
        }, indent=2)

    # Fix #2: when `caller.call_many` raises before its own finally clause
    # gets to run reporter.finalize, the on-disk frames are left with
    # non-terminal node states and the parent agent would see a stuck
    # spinner. `_finalize_at_boundary` rewrites them to ``Abandoned``
    # atomically. The happy path doesn't need this guard — call_many's
    # finalize chain (driver.run -> wait_for_terminal_signals ->
    # reporter.finalize) is already responsible for terminal state, and
    # running this on every successful call was per-tool-call disk I/O
    # for a guaranteed no-op.
    try:
        multi: MultiResult = await asyncio.to_thread(
            caller.call_many, tasks, context=context,
        )
    except Exception:
        _finalize_at_boundary(
            log_dir, invoke_id,
            reason="call_many raised before recording terminal frame state",
        )
        raise
    return json.dumps(
        _envelope_from_multi(invoke_id, report_path, multi), indent=2,
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
    entry = _background_tasks.get(invoke_id)
    if entry is None:
        return json.dumps({
            "invoke_id": invoke_id,
            "status": "error",
            "error": f"no background call with invoke_id={invoke_id!r}",
        }, indent=2)
    task: asyncio.Task = entry["task"]
    report_path: str = entry["report_path"]
    # `log_dir` was stashed by `call(run_in_background=True)` so we can
    # run the atomic-emit guard from this side too. Older entries (from
    # before the field was added) fall back to None and skip the guard
    # — the synchronous `call` path always finalizes, so the only
    # entries missing this field were created by an older mcp_server.
    log_dir = entry.get("log_dir")
    try:
        # `shield` so a timeout here cancels only our wait, not the
        # underlying call_many — the orchestrator can poll again.
        multi: MultiResult = await asyncio.wait_for(
            asyncio.shield(task), timeout=timeout,
        )
    except asyncio.TimeoutError:
        # Still running — leave the entry in place and do NOT force-
        # finalize. The background task is healthy and will write its
        # own terminal frame when it completes.
        return json.dumps({
            "invoke_id": invoke_id,
            "report_path": report_path,
            "status": "pending",
        }, indent=2)
    except Exception as e:
        _background_tasks.pop(invoke_id, None)
        # The background call's reporter.finalize may or may not have
        # run before the exception propagated; force-terminate any
        # surviving non-terminal frames so the parent sees a clean
        # error envelope rather than a stuck-running canvas row.
        if log_dir is not None:
            _finalize_at_boundary(
                log_dir, invoke_id,
                reason="background call_many raised before terminal frame state",
            )
        return json.dumps({
            "invoke_id": invoke_id,
            "report_path": report_path,
            "status": "error",
            "error": f"background call raised: {type(e).__name__}: {e}",
        }, indent=2)
    _background_tasks.pop(invoke_id, None)
    return json.dumps(
        _envelope_from_multi(invoke_id, report_path, multi), indent=2,
    )


@mcp.tool()
async def resume(resume_session: str, user_reply: str,
                 timeout: int = 300, cwd: str = "") -> str:
    """Resume a previously yielded call session with the user's reply.

    Use after a call returned status 'yield' — pass back the session_id and the
    user's answer. The clone_path comes from the same yield envelope."""
    invoke_id, log_dir = _invocation_identity(cwd)
    caller = _build_caller("", "", cwd, timeout, invoke_id, log_dir)
    # Locate the clone path so we can construct a YieldToken.
    from agent_callstack.session import SessionLocator
    clone = SessionLocator().resolve(resume_session, cwd=cwd or None)
    envelope = {"invoke_id": invoke_id, "report_path": _report_path(log_dir, invoke_id)}
    if clone is None:
        envelope.update({"status": "error",
                         "error": f"Cannot find session file for {resume_session}"})
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
