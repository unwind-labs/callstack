#!/usr/bin/env python3
"""
callstack.py — Call stack runtime for agent-callstack.

Implements function-call semantics for LLM agent orchestration:
1. Discover the active Claude Code session
2. Fork it via --fork-session (full context inheritance)
3. Spawn a new Claude Code instance using the stream-json protocol
4. Intercept permission requests via bidirectional NDJSON control protocol
5. Agent executes and returns a structured work description
6. Save audit trace, return result

Usage:
    python3 callstack.py --task "Implement auth module"
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple, Callable

def _format_error(error_msg: str, partial_output: Optional[str] = None, context: Optional[str] = None) -> str:
    """Format an error as JSON for the caller."""
    return json.dumps({
        "error": error_msg,
        "partial_result": partial_output,
        "context": context
    }, indent=2)

def parse_agent_output(output: str) -> dict:
    """Parse the three control instructions: CALL, YIELD, RETURN."""
    if "---CALL---" in output:
        parts = output.split("---CALL---", 1)
        task = parts[1].strip() if len(parts) > 1 else ""
        return {"status": "call", "task": task}
    if "---YIELD---" in output:
        parts = output.split("---YIELD---", 1)
        question = parts[1].strip() if len(parts) > 1 else ""
        return {"status": "yield", "question": question}
    if "---RETURN---" in output:
        parts = output.split("---RETURN---", 1)
        result = parts[1].strip() if len(parts) > 1 else output
        return {"status": "complete", "result": result}
    return {"status": "complete"}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
SYSTEM_INSTRUCTION = """\
You are running in a forked session — a child process that inherited the full context of \
your parent agent. Execute the task below.

You have three control instructions:

---CALL---
<what to accomplish>
Hand off work to a child process. Use CALL when the task ahead is \
multi-step, may involve its own chain of calls or user interaction, \
and you want only the result back — not the intermediate work. \
The child inherits your full context. Its execution trace is \
discarded; only its RETURN value comes back to you. \
Do your own work first, then CALL when you reach a point requiring \
a child. Don't CALL simple things you can do in one or two tool calls.

---YIELD---
<question for user>
Pause for user input. Only when you MUST have information that only \
the user can provide (e.g., MFA codes, passwords, confirmations). \
Do not guess. Stop after the YIELD marker.

---RETURN---
<your result>
Return from this task. This is your final output. Structure the \
result however is appropriate for the task.
"""

# ---------------------------------------------------------------------------
# Tree Data Structures
# ---------------------------------------------------------------------------

@dataclass
class TreeNode:
    """A node in the execution tree. Each node represents one agent invocation."""
    id: str
    task: str
    session_id: Optional[str] = None
    clone_path: Optional[str] = None
    parent_lines: int = 0
    status: str = "pending"  # pending | running | complete | yielded | error
    result: Optional[str] = None
    yield_question: Optional[str] = None
    yield_source: Optional[str] = None  # self.id if direct yield, child.id if blocked on child
    error: Optional[str] = None
    duration: float = 0.0
    children: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task": self.task,
            "session_id": self.session_id,
            "clone_path": self.clone_path,
            "parent_lines": self.parent_lines,
            "status": self.status,
            "result": self.result,
            "yield_question": self.yield_question,
            "yield_source": self.yield_source,
            "error": self.error,
            "duration": self.duration,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TreeNode":
        children = [cls.from_dict(c) for c in d.get("children", [])]
        return cls(
            id=d["id"],
            task=d["task"],
            session_id=d.get("session_id"),
            clone_path=d.get("clone_path"),
            parent_lines=d.get("parent_lines", 0),
            status=d.get("status", "pending"),
            result=d.get("result"),
            yield_question=d.get("yield_question"),
            yield_source=d.get("yield_source"),
            error=d.get("error"),
            duration=d.get("duration", 0.0),
            children=children,
        )


@dataclass
class ExecutionTree:
    """Top-level container for the execution tree."""
    root_session_id: str
    root_session_file: str
    call_depth_base: int
    nodes: list  # list[TreeNode] — root-level nodes

    def to_dict(self) -> dict:
        return {
            "root_session_id": self.root_session_id,
            "root_session_file": self.root_session_file,
            "call_depth_base": self.call_depth_base,
            "nodes": [n.to_dict() for n in self.nodes],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExecutionTree":
        return cls(
            root_session_id=d["root_session_id"],
            root_session_file=d["root_session_file"],
            call_depth_base=d["call_depth_base"],
            nodes=[TreeNode.from_dict(n) for n in d.get("nodes", [])],
        )


def _all_nodes(tree: ExecutionTree) -> list:
    """Flatten the tree into a list of all nodes (breadth-first)."""
    result = []
    queue = list(tree.nodes)
    while queue:
        node = queue.pop(0)
        result.append(node)
        queue.extend(node.children)
    return result


def _find_node_by_session(tree: ExecutionTree, session_id: str) -> Optional[TreeNode]:
    """Find a node by its session_id."""
    for node in _all_nodes(tree):
        if node.session_id == session_id:
            return node
    return None


def _find_yielded_leaf(node: TreeNode) -> TreeNode:
    """Follow yield_source chain to find the actual yielded leaf."""
    if node.yield_source == node.id or not node.yield_source:
        return node
    for child in node.children:
        if child.id == node.yield_source:
            return _find_yielded_leaf(child)
    return node


def _node_depth(node: TreeNode, tree: ExecutionTree) -> int:
    """Compute the depth of a node within the tree (0 for root-level nodes)."""
    def _find_depth(target_id, nodes, depth):
        for n in nodes:
            if n.id == target_id:
                return depth
            found = _find_depth(target_id, n.children, depth + 1)
            if found is not None:
                return found
        return None
    return _find_depth(node.id, tree.nodes, 0) or 0


def _find_parent_node(target: TreeNode, tree: ExecutionTree) -> Optional[TreeNode]:
    """Find the parent of a node in the tree."""
    def _search(target_id, nodes):
        for n in nodes:
            for child in n.children:
                if child.id == target_id:
                    return n
            found = _search(target_id, n.children)
            if found is not None:
                return found
        return None
    return _search(target.id, tree.nodes)


def _extract_cwd_from_session(session_file: Path) -> Optional[str]:
    """Read the cwd from the first message that has one in a session JSONL."""
    try:
        with open(session_file, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if 'cwd' in obj:
                        cwd = obj['cwd']
                        if os.path.isdir(cwd):
                            return cwd
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return None


ENV_CALLSTACK_DEPTH = "CALLSTACK_DEPTH"
ENV_CALLSTACK_PARENT_SESSION = "CALLSTACK_PARENT_SESSION"

# Env vars checked as fallback (in order)
SESSION_ID_ENV_VARS = [
    "CALLSTACK_PARENT_SESSION",    # Set by parent forked session (file path)
    "CLAUDE_SESSION_ID",            # Set by Claude Code inside skill bash commands
]

# ---------------------------------------------------------------------------
# Session Discovery
# ---------------------------------------------------------------------------

def get_cwd_project_dir() -> Optional[Path]:
    """
    Derive the project directory path in ~/.claude/projects/ from the current
    working directory, matching Claude Code's encoding scheme.
    """
    cwd = os.getcwd()
    # Claude Code encodes the path by replacing / with -
    encoded = cwd.replace("/", "-")
    # Remove leading dash
    if encoded.startswith("-"):
        pass  # keep it — Claude Code keeps the leading dash

    project_dir = PROJECTS_DIR / encoded
    if project_dir.is_dir():
        return project_dir

    # Try variations
    for d in PROJECTS_DIR.iterdir():
        if d.is_dir() and d.name != "memory":
            # Check if this directory's encoded path matches our cwd
            decoded = "/" + d.name.lstrip("-").replace("-", "/")
            if decoded == cwd or d.name == encoded:
                return d

    return None


def find_active_session_by_mtime(project_dir: Optional[Path] = None) -> Optional[Tuple[Path, str]]:
    """
    Strategy 1: Find the most recently modified session file.
    Works reliably for single-instance use.

    Returns (session_file_path, session_id) or None.
    """
    search_dirs = []

    if project_dir and project_dir.is_dir():
        search_dirs.append(project_dir)

    # Also search all project dirs as fallback
    if PROJECTS_DIR.is_dir():
        for d in sorted(PROJECTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime if p.is_dir() else 0, reverse=True):
            if d.is_dir() and d not in search_dirs:
                search_dirs.append(d)

    best_file = None
    best_mtime = 0

    for search_dir in search_dirs:
        for f in search_dir.glob("*.jsonl"):
            try:
                mtime = f.stat().st_mtime
                if mtime > best_mtime:
                    best_mtime = mtime
                    best_file = f
            except OSError:
                continue

    if best_file:
        session_id = best_file.stem
        return (best_file, session_id)

    return None


def resolve_session_file(session_id: str, cwd: Optional[str] = None) -> Optional[Path]:
    """
    Given a session ID (UUID), find the corresponding .jsonl file on disk.
    Searches the cwd-matching project dir first, then all project dirs.
    """
    # Try cwd-specific project dir first
    original_cwd = os.getcwd()
    if cwd:
        try:
            os.chdir(cwd)
        except OSError:
            pass

    project_dir = get_cwd_project_dir()

    if cwd:
        try:
            os.chdir(original_cwd)
        except OSError:
            pass

    if project_dir:
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.is_file():
            return candidate

    # Search all project dirs
    if PROJECTS_DIR.is_dir():
        for d in PROJECTS_DIR.iterdir():
            if d.is_dir():
                candidate = d / f"{session_id}.jsonl"
                if candidate.is_file():
                    return candidate

    return None


def discover_session(explicit_session_id: Optional[str] = None, cwd: Optional[str] = None) -> Tuple[Path, str]:
    """
    Discover the active Claude Code session.

    Strategy priority:
    1. Explicit --session-id argument (most reliable)
    2. Environment variables (CALLSTACK_PARENT_SESSION, CLAUDE_SESSION_ID)
    3. Most recent .jsonl by mtime in the cwd-matching project dir

    Returns (session_file_path, session_id).
    Raises RuntimeError if no session found.
    """
    # Strategy 1: Explicit session ID passed as argument
    if explicit_session_id:
        # Could be a UUID or a file path
        p = Path(explicit_session_id)
        if p.is_file():
            return (p, p.stem)
        # Treat as UUID — find the file
        found = resolve_session_file(explicit_session_id, cwd)
        if found:
            return (found, explicit_session_id)
        raise RuntimeError(
            f"Explicit session ID '{explicit_session_id}' provided but no matching "
            f"session file found in {PROJECTS_DIR}"
        )

    # Strategy 2: Check known environment variables
    for env_var in SESSION_ID_ENV_VARS:
        value = os.environ.get(env_var)
        if not value:
            continue

        # CALLSTACK_PARENT_SESSION is a file path
        p = Path(value)
        if p.is_file():
            print(f"[callstack] Found session via {env_var} (file path)", file=sys.stderr)
            return (p, p.stem)

        # Others are UUIDs
        found = resolve_session_file(value, cwd)
        if found:
            print(f"[callstack] Found session via {env_var}={value[:8]}...", file=sys.stderr)
            return (found, value)

    # Strategy 3: mtime heuristic (last resort)
    print(f"[callstack] No session ID from --session-id or env. Falling back to mtime heuristic.",
          file=sys.stderr)

    original_cwd = os.getcwd()
    if cwd:
        try:
            os.chdir(cwd)
        except OSError:
            pass

    project_dir = get_cwd_project_dir()

    if cwd:
        try:
            os.chdir(original_cwd)
        except OSError:
            pass

    result = find_active_session_by_mtime(project_dir)

    if result is None:
        raise RuntimeError(
            "Could not discover active Claude Code session.\n"
            f"Searched: {PROJECTS_DIR}\n"
            "No --session-id provided, no session env vars found, and mtime heuristic failed.\n"
            "Pass --session-id <uuid> or ensure Claude Code is running with an active session."
        )

    session_file, session_id = result
    return (session_file, session_id)



# Stream-JSON Invocation (replaces --print with bidirectional NDJSON protocol)
# ---------------------------------------------------------------------------

def _write_ndjson(stdin, obj: dict) -> None:
    """Write a single NDJSON line to a subprocess stdin."""
    stdin.write(json.dumps(obj) + "\n")
    stdin.flush()


def _default_permission_handler(tool_name: str, input_data: dict) -> dict:
    """Default permission handler: allow all tools (with logging)."""
    print(f"[callstack] Permission: allowing {tool_name}", file=sys.stderr)
    return {"behavior": "allow", "updatedInput": input_data}


def _stream_json_session(
    source_session_id: str,
    prompt_text: str,
    timeout: int = 300,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    permission_mode: Optional[str] = None,
    env: Optional[dict] = None,
    fork: bool = True,
    permission_handler: Optional[Callable] = None,
) -> Tuple[str, Optional[str], dict]:
    """
    Run a Claude session using the stream-json bidirectional protocol.

    This replaces the --print mode with structured NDJSON over stdin/stdout,
    enabling programmatic permission control via the control protocol.

    Returns (output_text, session_id, result_metadata).
    """
    handler = permission_handler or _default_permission_handler

    cmd = [
        "claude",
        "--output-format", "stream-json",
        "--input-format", "stream-json",
        "--verbose",
        "--resume", source_session_id,
        "--permission-prompt-tool", "stdio",
        "--permission-mode", permission_mode or "default",
    ]
    if fork:
        cmd.append("--fork-session")
    if model:
        cmd.extend(["--model", model])

    effective_cwd = cwd or os.getcwd()
    process_env = os.environ.copy()
    if env:
        process_env.update(env)

    log_id = uuid.uuid4().hex[:8]
    log_path = f"/tmp/callstack_{source_session_id[:8]}_{log_id}.log"
    print(f"[callstack] stream-json session (fork={fork}, source={source_session_id[:8]}..., "
          f"cwd={effective_cwd}, log={log_path})", file=sys.stderr)

    def _log(msg: str) -> None:
        """Write a timestamped debug line to the log file."""
        ts = time.strftime("%H:%M:%S")
        log_file.write(f"[{ts}] {msg}\n")
        log_file.flush()

    log_file = open(log_path, 'w')
    _log(f"cmd: {' '.join(cmd)}")
    _log(f"cwd: {effective_cwd}")
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=effective_cwd,
            env=process_env,
        )
    except Exception as e:
        _log(f"Popen failed: {e}")
        log_file.close()
        raise RuntimeError(f"Failed to start claude CLI: {e}") from e

    _log(f"Process started, pid={proc.pid}")

    # Read stderr in a background thread so it doesn't block stdout reads
    stderr_lines: list[str] = []
    def _stderr_reader():
        for line in proc.stderr:
            stderr_lines.append(line)
            _log(f"STDERR: {line.rstrip()}")
    stderr_thread = threading.Thread(target=_stderr_reader, daemon=True)
    stderr_thread.start()

    # Timeout watchdog — _killed_by_timeout distinguishes timeout from normal exit
    _killed_by_timeout = False
    _cancel_watchdog = threading.Event()
    def _watchdog():
        nonlocal _killed_by_timeout
        if not _cancel_watchdog.wait(timeout):
            # Timeout expired before cancellation
            _killed_by_timeout = True
            _log(f"TIMEOUT after {timeout}s — killing pid {proc.pid}")
            try:
                proc.kill()
            except OSError:
                pass
    watchdog = threading.Thread(target=_watchdog, daemon=True)
    watchdog.start()

    output_parts: list[str] = []
    session_id: Optional[str] = None
    result_metadata: dict = {}

    try:
        # Step 1: Send initialize request
        req_id = f"req_init_{uuid.uuid4().hex[:8]}"
        _log("Sending initialize request")
        _write_ndjson(proc.stdin, {
            "type": "control_request",
            "request_id": req_id,
            "request": {"subtype": "initialize", "hooks": None},
        })

        # Step 2: Send user message with the task
        _log(f"Sending user message ({len(prompt_text)} chars)")
        _write_ndjson(proc.stdin, {
            "type": "user",
            "session_id": "",
            "message": {"role": "user", "content": prompt_text},
            "parent_tool_use_id": None,
        })
        _log("Waiting for stdout messages...")

        # Step 3: Read NDJSON messages from stdout
        # NOTE: Use readline() not `for line in proc.stdout:` — the iterator
        # uses a read-ahead buffer that delays line delivery, causing hangs.
        line_count = 0
        last_activity = time.time()
        while True:
            raw_line = proc.stdout.readline()
            if not raw_line:
                _log("EOF on stdout")
                break  # EOF — process closed stdout
            now = time.time()
            if now - last_activity > 10:
                _log(f"Activity after {now - last_activity:.1f}s gap")
            last_activity = now
            raw_line = raw_line.strip()
            if not raw_line:
                continue

            line_count += 1
            try:
                msg = json.loads(raw_line)
            except json.JSONDecodeError:
                _log(f"Unparseable line #{line_count}: {raw_line[:200]}")
                continue

            msg_type = msg.get("type")
            msg_subtype = msg.get("subtype", msg.get("request", {}).get("subtype", "-"))
            _log(f"stdout #{line_count}: type={msg_type}, subtype={msg_subtype}"
                 + (f", error={msg.get('error')}" if msg.get("error") else ""))

            # Handle control_response (response to our initialize request)
            if msg_type == "control_response":
                resp = msg.get("response", {})
                if resp.get("request_id") == req_id:
                    pass  # init handshake complete
                continue

            # Handle control_request (permission requests from CLI)
            if msg_type == "control_request":
                request_data = msg.get("request", {})
                request_id = msg.get("request_id", "")
                subtype = request_data.get("subtype", "")

                if subtype == "can_use_tool":
                    tool_name = request_data.get("tool_name", "")
                    tool_input = request_data.get("input", {})
                    _log(f"Permission request: {tool_name}")
                    resp_data = handler(tool_name, tool_input)
                    _write_ndjson(proc.stdin, {
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": request_id,
                            "response": resp_data,
                        },
                    })
                else:
                    # Unknown control request — send empty success
                    _write_ndjson(proc.stdin, {
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": request_id,
                            "response": {},
                        },
                    })
                continue

            # Collect assistant text output
            if msg_type == "assistant":
                content = msg.get("message", {}).get("content", [])
                for block in content:
                    if block.get("type") == "text":
                        output_parts.append(block["text"])

            # Capture result metadata and session_id — result means session is done
            if msg_type == "result":
                session_id = msg.get("session_id")
                result_metadata = {
                    "duration_ms": msg.get("duration_ms"),
                    "total_cost_usd": msg.get("total_cost_usd"),
                    "num_turns": msg.get("num_turns"),
                    "is_error": msg.get("is_error", False),
                    "result_text": msg.get("result"),
                }
                _log(f"Result received, session_id={session_id}")
                break  # Session complete — don't wait for EOF

        # Terminate the process (it may linger after sending result)
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            proc.kill()
            proc.wait()
        _log(f"Process exited with code {proc.returncode}")

    except Exception as e:
        try:
            proc.kill()
            proc.wait()
        except OSError:
            pass
        raise
    finally:
        # Cancel watchdog
        _cancel_watchdog.set()
        log_file.close()

    if _killed_by_timeout:
        output_text = "".join(output_parts)
        raise TimeoutError(f"stream-json session timed out after {timeout}s", output_text)

    output_text = "".join(output_parts)

    # If result message had the text, prefer it (it's the final compacted result)
    if result_metadata.get("result_text") and not output_text:
        output_text = result_metadata["result_text"]

    if proc.returncode != 0 and not output_text:
        with open(log_path, 'r') as f:
            stderr_content = f.read()
        output_text = stderr_content or f"stream-json session exited with code {proc.returncode}"

    return (output_text, session_id, result_metadata)


def invoke_streaming(
    source_session_id: str,
    task: str,
    original_session_file: Path,
    timeout: int = 300,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    permission_mode: Optional[str] = None,
    call_depth: int = 1,
    max_depth: int = 5,
    resume_mode: bool = False,
    resume_reply: Optional[str] = None,
    permission_handler: Optional[Callable] = None,
) -> Tuple[str, Optional[str]]:
    """
    Invoke a Claude session using the stream-json protocol.

    Replaces invoke() + clone_session() — the CLI handles forking via --fork-session.
    Returns (output_text, forked_session_id).
    """
    if resume_mode:
        prompt = resume_reply or "Continue your task."
        fork = False  # Resume the existing session, don't fork again
    else:
        prompt = SYSTEM_INSTRUCTION + "\n\n## Task\n\n" + task
        fork = True  # Fork to create an independent copy

    env_vars = {
        ENV_CALLSTACK_DEPTH: str(call_depth),
        ENV_CALLSTACK_PARENT_SESSION: str(original_session_file),
    }

    # Derive cwd from session file if not provided
    effective_cwd = cwd
    if not effective_cwd:
        effective_cwd = _extract_cwd_from_session(original_session_file)
    if not effective_cwd:
        effective_cwd = os.getcwd()

    output, session_id, metadata = _stream_json_session(
        source_session_id=source_session_id,
        prompt_text=prompt,
        timeout=timeout,
        cwd=effective_cwd,
        model=model,
        permission_mode=permission_mode,
        env=env_vars,
        fork=fork,
        permission_handler=permission_handler,
    )

    return (output, session_id)




# ---------------------------------------------------------------------------
# Call Stack Tracing
# ---------------------------------------------------------------------------

def write_trace(trace_dir: Path, call_depth: int, task: str,
                session_id: str, result: str, duration: float, error: Optional[str] = None):
    """Write a call trace entry for debugging/auditing."""
    trace_dir.mkdir(parents=True, exist_ok=True)

    trace_entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "call_depth": call_depth,
        "session_id": session_id,
        "task": task[:200],
        "duration_seconds": round(duration, 2),
        "result_length": len(result),
        "error": error,
    }

    trace_file = trace_dir / "call_trace.jsonl"
    with open(trace_file, 'a') as f:
        f.write(json.dumps(trace_entry) + '\n')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Call Stack Management (Tree-based)
# ---------------------------------------------------------------------------

def _save_tree(tree: ExecutionTree, yielded_clone_path: Path) -> Path:
    """Persist execution tree to a sidecar JSON file for resume after YIELD.

    Stored next to the yielded leaf's clone so --resume-session can find it.
    """
    sidecar_path = Path(str(yielded_clone_path) + ".call_tree")
    with open(sidecar_path, 'w') as f:
        json.dump(tree.to_dict(), f, indent=2)
    print(f"[callstack] Saved tree to {sidecar_path.name}", file=sys.stderr)
    return sidecar_path


def _load_tree(clone_path: Path) -> Optional[ExecutionTree]:
    """Load execution tree from sidecar file. Returns tree or None."""
    sidecar_path = Path(str(clone_path) + ".call_tree")
    if not sidecar_path.exists():
        return None
    with open(sidecar_path, 'r') as f:
        data = json.load(f)
    sidecar_path.unlink()
    return ExecutionTree.from_dict(data)


def _run_node(node: TreeNode, source_session_file: Path, args, tree: ExecutionTree, trace_dir: Path):
    """
    Execute a single tree node. The core recursive executor.

    Clone source_session_file, invoke the agent, parse output:
    - CALL: create child node, recurse, resume self with child result, re-parse
    - YIELD: park this node, return
    - RETURN/complete: mark complete, return

    Mutates node in place.
    """
    depth = tree.call_depth_base + _node_depth(node, tree)
    source_session_id = source_session_file.stem
    node.status = "running"
    print(f"[callstack] Node {node.id[:8]} forking from {source_session_id[:8]}... (depth={depth})",
          file=sys.stderr)

    # Count parent lines before forking so callers know where the child's work starts
    try:
        with open(source_session_file, 'r') as f:
            node.parent_lines = sum(1 for _ in f)
    except OSError:
        node.parent_lines = 0

    # --- First invocation (--fork-session creates an independent copy) ---
    start = time.time()
    try:
        output, forked_id = invoke_streaming(
            source_session_id=source_session_id,
            task=node.task,
            original_session_file=source_session_file,
            timeout=args.timeout,
            cwd=args.cwd,
            model=args.model,
            permission_mode=args.permission_mode,
            call_depth=depth,
            max_depth=args.max_depth,
        )
        node.session_id = forked_id
        # Resolve clone_path for child CALLs and YIELD persistence
        if forked_id:
            resolved = resolve_session_file(forked_id, cwd=args.cwd)
            node.clone_path = str(resolved) if resolved else None
    except TimeoutError as e:
        node.duration = time.time() - start
        node.status = "error"
        node.error = str(e.args[0]) if e.args else "Timeout"
        node.result = e.args[1] if len(e.args) > 1 else None
        write_trace(trace_dir, depth, node.task, node.session_id or "unknown",
                    node.error, node.duration, node.error)
        return
    except Exception as e:
        node.duration = time.time() - start
        node.status = "error"
        node.error = f"Invocation failed: {e}"
        write_trace(trace_dir, depth, node.task, node.session_id or "unknown",
                    node.error, node.duration, node.error)
        return

    node.duration = time.time() - start
    write_trace(trace_dir, depth, node.task, node.session_id or "unknown", output, node.duration)

    # --- Parse-and-loop: handle CALL chains within this node ---
    node_session_file = Path(node.clone_path) if node.clone_path else source_session_file
    node_session_id = node.session_id or source_session_id

    while True:
        parsed = parse_agent_output(output)

        if parsed["status"] == "complete":
            node.status = "complete"
            node.result = parsed.get("result", output)
            return

        elif parsed["status"] == "yield":
            node.status = "yielded"
            node.yield_question = parsed["question"]
            node.yield_source = node.id  # direct yield
            return

        elif parsed["status"] == "call":
            # Create child node and recurse
            child = TreeNode(id=str(uuid.uuid4()), task=parsed["task"])
            node.children.append(child)

            _run_node(child, node_session_file, args, tree, trace_dir)

            if child.status == "yielded":
                # Propagate yield up
                node.status = "yielded"
                node.yield_question = child.yield_question
                node.yield_source = child.id
                return

            if child.status == "error":
                node.status = "error"
                node.error = f"Child failed: {child.error}"
                return

            # Child completed — resume this node with child's result
            start = time.time()
            try:
                output, _ = invoke_streaming(
                    source_session_id=node_session_id,
                    task="(child-returned)",
                    original_session_file=node_session_file,
                    timeout=args.timeout,
                    cwd=args.cwd,
                    model=args.model,
                    permission_mode=args.permission_mode,
                    call_depth=depth,
                    max_depth=args.max_depth,
                    resume_mode=True,
                    resume_reply=(
                        "Your child completed. Here is the result:\n\n"
                        + (child.result or "")
                    ),
                )
            except Exception as e:
                node.duration += time.time() - start
                node.status = "error"
                node.error = f"Resume after child failed: {e}"
                return

            node.duration += time.time() - start
            write_trace(trace_dir, depth, "(child-returned)", node_session_id, output, node.duration)
            # Loop back to parse this new output


# ---------------------------------------------------------------------------
# Delegation Loop (replaces nested blocking for interactive calls)
# ---------------------------------------------------------------------------

def run_tree(args, session_file: Path, session_id: str, call_depth: int) -> str:
    """
    Unified entry point for tree-based execution.

    --task → 1 root node, run directly.
    --tasks → N root nodes, run via ThreadPoolExecutor.
    Every branch supports CALL/YIELD/RETURN.

    Returns JSON string.
    """
    import concurrent.futures

    tasks = args.tasks if args.tasks else [args.task]
    trace_dir = Path(args.trace_dir) if args.trace_dir else (session_file.parent / "call_traces")

    tree = ExecutionTree(
        root_session_id=session_id,
        root_session_file=str(session_file),
        call_depth_base=call_depth,
        nodes=[TreeNode(id=str(uuid.uuid4()), task=t) for t in tasks],
    )

    if len(tree.nodes) == 1:
        _run_node(tree.nodes[0], session_file, args, tree, trace_dir)
    else:
        print(f"[callstack] Launching {len(tree.nodes)} parallel agents...", file=sys.stderr)
        start_all = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(tree.nodes)) as executor:
            futures = {
                executor.submit(_run_node, node, session_file, args, tree, trace_dir): node
                for node in tree.nodes
            }
            concurrent.futures.wait(futures)
        total_dur = time.time() - start_all
        print(f"[callstack] All {len(tree.nodes)} parallel agents completed in {total_dur:.1f}s",
              file=sys.stderr)

    # Collect results
    yielded = [n for n in tree.nodes if n.status == "yielded"]

    if yielded:
        # Save tree next to each yielded leaf's clone for resume discovery
        for yn in yielded:
            leaf = _find_yielded_leaf(yn)
            if leaf.clone_path:
                _save_tree(tree, Path(leaf.clone_path))

        if len(tree.nodes) == 1:
            leaf = _find_yielded_leaf(yielded[0])
            return json.dumps({
                "status": "yield",
                "question": leaf.yield_question,
                "session_id": leaf.session_id,
                "clone_path": leaf.clone_path,
                "parent_lines": leaf.parent_lines,
                "duration": round(yielded[0].duration, 2),
            })
        else:
            # Parallel mode with yields — return array with mixed statuses
            results = []
            for i, node in enumerate(tree.nodes):
                entry = {
                    "index": i,
                    "task": node.task[:100],
                    "result": node.result,
                    "error": node.error,
                    "duration": round(node.duration, 1),
                    "session_log": node.clone_path,
                    "session_log_start_line": node.parent_lines + 1,
                }
                if node.status == "yielded":
                    leaf = _find_yielded_leaf(node)
                    entry["status"] = "yielded"
                    entry["yield_question"] = leaf.yield_question
                    entry["session_id"] = leaf.session_id
                else:
                    entry["status"] = node.status
                results.append(entry)
            return json.dumps(results, indent=2)

    # All complete (or errored)
    if len(tree.nodes) == 1:
        node = tree.nodes[0]
        return json.dumps({
            "status": node.status,
            "result": node.result,
            "error": node.error,
            "duration": round(node.duration, 2),
            "session_log": node.clone_path,
            "session_log_start_line": node.parent_lines + 1,
        })
    else:
        return json.dumps([
            {
                "index": i,
                "task": n.task[:100],
                "result": n.result,
                "error": n.error,
                "duration": round(n.duration, 1),
                "session_log": n.clone_path,
                "session_log_start_line": n.parent_lines + 1,
            }
            for i, n in enumerate(tree.nodes)
        ], indent=2)


def _resume_node(node: TreeNode, reply: str, args, tree: ExecutionTree, trace_dir: Path):
    """
    Resume a yielded node with the user's reply. Continues the parse loop.
    Mutates node in place.
    """
    depth = tree.call_depth_base + _node_depth(node, tree)
    node_session_id = node.session_id or "unknown"
    node_session_file = Path(node.clone_path) if node.clone_path else Path(".")

    start = time.time()
    try:
        output, _ = invoke_streaming(
            source_session_id=node_session_id,
            task="(resumed)",
            original_session_file=node_session_file,
            timeout=args.timeout,
            cwd=args.cwd,
            model=args.model,
            permission_mode=args.permission_mode,
            call_depth=depth,
            max_depth=args.max_depth,
            resume_mode=True,
            resume_reply=reply,
        )
    except Exception as e:
        node.duration += time.time() - start
        node.status = "error"
        node.error = f"Resume failed: {e}"
        return

    node.duration += time.time() - start
    write_trace(trace_dir, depth, "(resumed)", node_session_id, output, node.duration)

    # Same parse-and-loop as _run_node
    while True:
        parsed = parse_agent_output(output)

        if parsed["status"] == "complete":
            node.status = "complete"
            node.result = parsed.get("result", output)
            node.yield_question = None
            node.yield_source = None
            return

        elif parsed["status"] == "yield":
            node.status = "yielded"
            node.yield_question = parsed["question"]
            node.yield_source = node.id
            return

        elif parsed["status"] == "call":
            child = TreeNode(id=str(uuid.uuid4()), task=parsed["task"])
            node.children.append(child)

            _run_node(child, node_session_file, args, tree, trace_dir)

            if child.status == "yielded":
                node.status = "yielded"
                node.yield_question = child.yield_question
                node.yield_source = child.id
                return

            if child.status == "error":
                node.status = "error"
                node.error = f"Child failed: {child.error}"
                return

            # Child completed — resume with result
            start = time.time()
            try:
                output, _ = invoke_streaming(
                    source_session_id=node_session_id,
                    task="(child-returned)",
                    original_session_file=node_session_file,
                    timeout=args.timeout,
                    cwd=args.cwd,
                    model=args.model,
                    permission_mode=args.permission_mode,
                    call_depth=depth,
                    max_depth=args.max_depth,
                    resume_mode=True,
                    resume_reply=(
                        "Your child completed. Here is the result:\n\n"
                        + (child.result or "")
                    ),
                )
            except Exception as e:
                node.duration += time.time() - start
                node.status = "error"
                node.error = f"Resume after child failed: {e}"
                return

            node.duration += time.time() - start
            write_trace(trace_dir, depth, "(child-returned)", node_session_id, output, node.duration)
            # Loop back to parse


def _unwind_completed_nodes(tree: ExecutionTree, args, trace_dir: Path):
    """
    Walk the tree bottom-up. If a node was yielded because its child yielded
    (yield_source != node.id), and that child is now complete, resume the
    parent with the child's result. Repeat until no more progress.
    """
    changed = True
    while changed:
        changed = False
        for node in _all_nodes(tree):
            if node.status != "yielded":
                continue
            if node.yield_source == node.id:
                continue  # node itself yielded, not blocked on child

            # Find the child we were blocked on
            blocked_child = None
            for child in node.children:
                if child.id == node.yield_source:
                    blocked_child = child
                    break

            if blocked_child is None:
                continue

            if blocked_child.status == "complete":
                # Child finished — resume this parent
                clone_path = Path(node.clone_path)
                _resume_node(
                    node,
                    "Your child completed. Here is the result:\n\n" + (blocked_child.result or ""),
                    args, tree, trace_dir,
                )
                changed = True
            elif blocked_child.status == "error":
                node.status = "error"
                node.error = f"Child failed: {blocked_child.error}"
                changed = True


def run_resume(args) -> str:
    """Resume a yielded tree. Replaces run_resume_call."""
    session_id = args.resume_session
    reply = args.user_reply

    clone_path = resolve_session_file(session_id, cwd=args.cwd)
    if clone_path is None:
        return json.dumps({
            "status": "error",
            "error": f"Cannot find clone session file for {session_id}"
        })

    trace_dir = Path(args.trace_dir) if args.trace_dir else (clone_path.parent / "call_traces")

    # Load the execution tree
    tree = _load_tree(clone_path)

    if tree is None:
        # No tree — simple single-node resume (backward compat)
        current_depth = int(os.environ.get(ENV_CALLSTACK_DEPTH, "0"))
        node = TreeNode(
            id=str(uuid.uuid4()),
            task="(resumed)",
            session_id=session_id,
            clone_path=str(clone_path),
            parent_lines=args.parent_lines or 0,
            status="yielded",
            yield_source=None,
        )
        tree = ExecutionTree(
            root_session_id=session_id,
            root_session_file=str(clone_path),
            call_depth_base=current_depth + 1,
            nodes=[node],
        )
        _resume_node(node, reply, args, tree, trace_dir)

        if node.status == "yielded":
            leaf = _find_yielded_leaf(node)
            if leaf.clone_path:
                _save_tree(tree, Path(leaf.clone_path))
            return json.dumps({
                "status": "yield",
                "question": leaf.yield_question,
                "session_id": leaf.session_id,
                "clone_path": leaf.clone_path,
                "parent_lines": leaf.parent_lines,
                "duration": round(node.duration, 2),
            })

        return json.dumps({
            "status": node.status,
            "result": node.result,
            "error": node.error,
            "duration": round(node.duration, 2),
        })

    # Tree exists — find the yielded node and resume it
    yielded_node = _find_node_by_session(tree, session_id)
    if yielded_node is None:
        return json.dumps({
            "status": "error",
            "error": f"Session {session_id} not found in execution tree"
        })

    _resume_node(yielded_node, reply, args, tree, trace_dir)

    # Unwind: if the resumed node completed, its parent may be unblocked
    _unwind_completed_nodes(tree, args, trace_dir)

    # Check if any nodes are still yielded
    all_yielded = [n for n in _all_nodes(tree) if n.status == "yielded" and n.yield_source == n.id]

    if all_yielded:
        # Save tree for next resume
        for yn in all_yielded:
            if yn.clone_path:
                _save_tree(tree, Path(yn.clone_path))

        leaf = all_yielded[0]
        return json.dumps({
            "status": "yield",
            "question": leaf.yield_question,
            "session_id": leaf.session_id,
            "clone_path": leaf.clone_path,
            "parent_lines": leaf.parent_lines,
            "duration": round(leaf.duration, 2),
        })

    # All done — find the root-level result
    if len(tree.nodes) == 1:
        node = tree.nodes[0]
        return json.dumps({
            "status": node.status,
            "result": node.result,
            "error": node.error,
            "duration": round(node.duration, 2),
        })
    else:
        return json.dumps([
            {
                "index": i,
                "task": n.task[:100],
                "result": n.result,
                "error": n.error,
                "duration": round(n.duration, 1),
            }
            for i, n in enumerate(tree.nodes)
        ], indent=2)


def main():
    parser = argparse.ArgumentParser(
        description="agent-callstack — call stack runtime for LLM agent orchestration"
    )
    parser.add_argument(
        "--session-id", type=str, default=None,
        help="Session ID (UUID) or file path of the parent session to clone. "
             "If omitted, auto-discovered from env vars or mtime heuristic."
    )
    parser.add_argument(
        "--task", type=str, default=None,
        help="The task to execute (single mode)"
    )
    parser.add_argument(
        "--tasks", type=str, nargs="+", default=None,
        help="Multiple tasks to execute in parallel (parallel mode)"
    )
    parser.add_argument(
        "--timeout", type=int, default=300,
        help="Max seconds per agent before kill (default: 300)"
    )
    parser.add_argument(
        "--max-depth", type=int, default=5,
        help="Max call nesting depth (default: 5)"
    )
    parser.add_argument(
        "--cwd", type=str, default=None,
        help="Working directory for the agent"
    )
    parser.add_argument(
        "--model", type=str, default=None,
        help="Model override (e.g. sonnet, opus)"
    )
    parser.add_argument(
        "--permission-mode", type=str, default=None,
        help="Permission mode (default: auto)"
    )
    parser.add_argument(
        "--trace-dir", type=str, default=None,
        help="Directory for call trace logs"
    )
    parser.add_argument(
        "--resume-session", type=str, default=None,
        help="Resume a previously paused session (clone session ID)"
    )
    parser.add_argument(
        "--user-reply", type=str, default=None,
        help="The user's reply to inject when resuming (required with --resume-session)"
    )



    args = parser.parse_args()

    # --- Resume mode (takes priority) ---
    if args.resume_session:
        if not args.user_reply:
            parser.error("--user-reply is required with --resume-session")
        if args.task or args.tasks:
            parser.error("--resume-session cannot be used with --task or --tasks")
        result = run_resume(args)
        print(result)
        return

    if not args.task and not args.tasks:
        parser.error("Either --task, --tasks, or --resume-session is required")

    if args.task and args.tasks:
        parser.error("Use --task for single mode OR --tasks for parallel mode, not both")

    # --- Depth check ---
    current_depth = int(os.environ.get(ENV_CALLSTACK_DEPTH, "0"))
    call_depth = current_depth + 1

    if call_depth > args.max_depth:
        error_result = _format_error(
            f"Maximum call depth ({args.max_depth}) exceeded. Current depth: {call_depth}.",
            context=f"Task was: {(args.task or str(args.tasks))[:200]}"
        )
        print(error_result)
        sys.exit(1)

    # --- Session discovery ---
    try:
        session_file, session_id = discover_session(
            explicit_session_id=args.session_id, cwd=args.cwd
        )
        print(f"[callstack] Discovered session: {session_id[:8]}... at {session_file}",
              file=sys.stderr)
    except RuntimeError as e:
        print(_format_error(str(e)), file=sys.stdout)
        sys.exit(1)

    # --- Execute via tree scheduler (handles both --task and --tasks) ---
    result = run_tree(args, session_file, session_id, call_depth)
    print(result)


if __name__ == "__main__":
    main()
