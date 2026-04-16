"""
MCP server that wraps callstack.py as named tools.

Exposes `invoke`, `invoke_parallel`, and `invoke_resume` as MCP tools so
they render with proper tool names in Claude Code instead of Bash(python3 ...).

- `invoke` — single task, shown individually in the UI
- `invoke_parallel` — multiple tasks run concurrently with true parallelism
- `invoke_resume` — resume a yielded session

All tool handlers are async to avoid blocking the event loop.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("call")

CALLSTACK_PY = str(Path(__file__).parent / "callstack.py")


async def _run_callstack(*args: str) -> str:
    """Run callstack.py with the given arguments and return stdout (async)."""
    cmd = [sys.executable, CALLSTACK_PY, *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ,
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode() if stdout else ""
    if proc.returncode != 0 and not output:
        return json.dumps({
            "error": f"callstack.py exited with code {proc.returncode}",
            "stderr": (stderr.decode()[-2000:]) if stderr else None,
        })
    return output


@mcp.tool()
async def invoke(task: str, timeout: int = 300, session_id: str = "", model: str = "", cwd: str = "") -> str:
    """Fork a sub-agent to execute a task. The child inherits the parent's full
    conversation context. Only the result comes back — intermediate work is discarded.

    Use for substantial multi-step tasks, not simple one-off commands."""
    args = ["--task", task, "--timeout", str(timeout)]
    if session_id:
        args.extend(["--session-id", session_id])
    if model:
        args.extend(["--model", model])
    if cwd:
        args.extend(["--cwd", cwd])
    return await _run_callstack(*args)


@mcp.tool()
async def invoke_parallel(tasks: list[str], timeout: int = 300, session_id: str = "", model: str = "", cwd: str = "") -> str:
    """Fork multiple sub-agents to execute tasks concurrently. Each gets the parent's
    full context. Results are collected when all complete.

    Use when tasks are independent and need true parallelism."""
    args = ["--tasks"] + tasks + ["--timeout", str(timeout)]
    if session_id:
        args.extend(["--session-id", session_id])
    if model:
        args.extend(["--model", model])
    if cwd:
        args.extend(["--cwd", cwd])
    return await _run_callstack(*args)


@mcp.tool()
async def invoke_resume(resume_session: str, user_reply: str, timeout: int = 300, cwd: str = "") -> str:
    """Resume a previously yielded call session with the user's reply.

    Use after a call returned status 'yield' — pass back the session_id and the user's answer."""
    args = ["--resume-session", resume_session, "--user-reply", user_reply, "--timeout", str(timeout)]
    if cwd:
        args.extend(["--cwd", cwd])
    return await _run_callstack(*args)


if __name__ == "__main__":
    mcp.run()
