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
from pathlib import Path
from typing import Any, Optional

from agent_callstack import (
    Caller, CallFailed, CallYielded, MultiResult, Result, YieldToken,
    _new_invoke_id,
)
from agent_callstack import ENV_ROOT_INVOKE_ID, ENV_ROOT_LOG_DIR
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("call")


def _result_to_dict(item: Any) -> dict:
    """Translate a Result / CallYielded / CallFailed into the wire envelope."""
    if isinstance(item, Result):
        return {
            "status": "complete",
            "result": item.value,
            "summary": item.summary,
            "suggested_next": item.next,
            "duration": item.duration,
            "session_log": str(item.log) if item.log else None,
            "session_log_start_line": item.log_start,
        }
    if isinstance(item, CallYielded):
        return {
            "status": "yield",
            "question": item.question,
            "session_id": item.token.session_id,
            "clone_path": item.token.clone_path,
        }
    if isinstance(item, CallFailed):
        return {
            "status": "error",
            "error": item.error,
            "partial_result": item.partial,
        }
    raise TypeError(f"unknown result type: {type(item).__name__}")


def _log_dir(cwd: str) -> Path:
    return Path(cwd or os.getcwd()) / ".claude" / "callstack" / "log"


def _report_path(log_dir: Path, invoke_id: str) -> str:
    return str(log_dir / invoke_id / "report.yaml")


def _invocation_identity(cwd: str) -> tuple[str, Path]:
    """Return the (invoke_id, log_dir) this MCP call will actually write to.

    If the process env carries `CALLSTACK_ROOT_*` it means we're running
    inside an already-live invocation (nested MCP call) — reuse that root's
    identity so our work merges into its report.yaml rather than spawning
    a fresh top-level invocation. Otherwise mint a new id."""
    root_id = os.environ.get(ENV_ROOT_INVOKE_ID)
    root_dir = os.environ.get(ENV_ROOT_LOG_DIR)
    if root_id and root_dir:
        return root_id, Path(root_dir)
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


def _resolve_cwd(raw: str) -> tuple[str, Optional[str]]:
    """Expand `{PWD}` against the caller's project folder, then canonicalize.

    Returns (resolved_abs_path, error_message). On success error_message is
    None and resolved_abs_path is an absolute directory that exists. On
    failure resolved_abs_path may be a partial expansion (for error display)
    and error_message describes what went wrong.

    Empty `raw` resolves to the caller's project folder (the default behavior
    before this feature) — no validation needed in that case."""
    if not raw:
        return _parent_project_folder(), None
    expanded = raw.replace("{PWD}", _parent_project_folder())
    expanded = os.path.abspath(os.path.expanduser(expanded))
    if not os.path.isdir(expanded):
        return expanded, f"cwd '{expanded}' is not an existing directory"
    return expanded, None


def _same_project(a: str, b: str) -> bool:
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return False


@mcp.tool()
async def call(tasks: list[str], timeout: int = 300, session_id: str = "",
               model: str = "", cwd: str = "", context: str = "fork") -> str:
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
        with a different project folder returns an error envelope."""
    if context not in ("fork", "fresh"):
        return json.dumps({
            "invoke_id": "", "report_path": "",
            "results": [{"status": "error",
                         "error": f"invalid context: {context!r} "
                                  f"(must be 'fork' or 'fresh')"}],
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
    multi: MultiResult = await asyncio.to_thread(
        caller.call_many, tasks, context=context,
    )
    return json.dumps({
        "invoke_id": invoke_id,
        "report_path": _report_path(log_dir, invoke_id),
        "results": [_result_to_dict(r) for r in multi.results],
    }, indent=2)


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
