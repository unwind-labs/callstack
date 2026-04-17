"""MCP server: thin shim over agent_callstack.

Exposes `invoke`, `invoke_parallel`, and `invoke_resume` as MCP tools by
calling the runtime in-process. The previous version shelled out to
callstack.py via subprocess.exec — that round-trip is gone.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from agent_callstack import (
    Caller, CallFailed, CallYielded, MultiResult, Result, YieldToken,
)
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


def _build_caller(session: str, model: str, cwd: str, timeout: int) -> Caller:
    return Caller(
        session=session or None,
        model=model or None,
        cwd=cwd or None,
        timeout=timeout,
    )


@mcp.tool()
async def invoke(task: str, timeout: int = 300, session_id: str = "",
                 model: str = "", cwd: str = "") -> str:
    """Fork a sub-agent to execute `task`. The child inherits the parent's full
    conversation context. Only the result comes back — intermediate work is
    discarded. Use for substantial multi-step tasks, not simple commands."""
    caller = _build_caller(session_id, model, cwd, timeout)
    try:
        result = await asyncio.to_thread(caller.call, task)
        return json.dumps(_result_to_dict(result))
    except CallYielded as y:
        return json.dumps(_result_to_dict(y))
    except CallFailed as f:
        return json.dumps(_result_to_dict(f))


@mcp.tool()
async def invoke_parallel(tasks: list[str], timeout: int = 300, session_id: str = "",
                          model: str = "", cwd: str = "") -> str:
    """Fork multiple sub-agents to execute tasks concurrently. Each gets the
    parent's full context. Results are collected when all complete. Use when
    tasks are independent and need true parallelism."""
    caller = _build_caller(session_id, model, cwd, timeout)
    multi: MultiResult = await asyncio.to_thread(caller.call_many, tasks)
    return json.dumps([_result_to_dict(r) for r in multi.results], indent=2)


@mcp.tool()
async def invoke_resume(resume_session: str, user_reply: str,
                        timeout: int = 300, cwd: str = "") -> str:
    """Resume a previously yielded call session with the user's reply.

    Use after a call returned status 'yield' — pass back the session_id and the
    user's answer. The clone_path comes from the same yield envelope."""
    # Locate the clone path next to the session id by checking the sidecar file.
    # The yield envelope includes both `session_id` and `clone_path`; for
    # convenience we accept session_id alone and let the caller's resume() find
    # the sidecar via the locator.
    caller = _build_caller("", "", cwd, timeout)
    # Locate the clone path so we can construct a YieldToken.
    from agent_callstack.session import SessionLocator
    clone = SessionLocator().resolve(resume_session, cwd=cwd or None)
    if clone is None:
        return json.dumps({"status": "error",
                           "error": f"Cannot find session file for {resume_session}"})
    token = YieldToken(session_id=resume_session, clone_path=str(clone))
    try:
        result = await asyncio.to_thread(caller.resume, token, user_reply)
        return json.dumps(_result_to_dict(result))
    except CallYielded as y:
        return json.dumps(_result_to_dict(y))
    except CallFailed as f:
        return json.dumps(_result_to_dict(f))


if __name__ == "__main__":
    mcp.run()
