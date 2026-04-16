---
name: call
description: "Fork a sub-agent that inherits full conversation context, executes a task, and returns only the result — keeping the parent context clean. Use for multi-step work, implementation + testing combos, parallel independent subtasks, or any substantial work that would pollute context with intermediate steps."
when_to_use: "User gives a multi-part task (e.g. 'implement X then write tests'); task has independent subtasks that can run in parallel; task is complex enough that intermediate tool output would bloat context; user says 'call', 'fork', 'subtask', or 'delegate'."
---

# /call — Agent Call Stack

Use `/call` when you need to hand off substantial work to a forked session
while preserving your full conversation context. The forked session inherits
everything you know. Its execution trace is discarded — only the return
value comes back. Your context stays clean.

## When to use

- Complex sub-tasks that benefit from your full conversation history
- Work that would pollute your context with intermediate steps
- Nested workflows where sub-tasks may themselves need to call further
- Any task where quality would suffer from losing your context

## When NOT to use

- Simple, quick tasks — just do them directly
- Tasks that don't need conversation history
- Single shell commands
- Trivially short tasks where the overhead isn't worth it

## Invocation

Use the MCP tools from the `call` server. These render with proper tool
names in Claude Code (e.g. `call - invoke (MCP)` instead of `Bash(python3 ...)`).

### Single mode

Use the `invoke` MCP tool:

```
invoke(task="Implement the authentication module using the patterns we discussed", timeout=300)
```

### Parallel mode — multiple independent tasks

Use `invoke_parallel` for true concurrent execution via ThreadPoolExecutor:

```
invoke_parallel(tasks=["Authenticate customer Sarah Chen", "Look up order ord_91847", "Check inventory"], timeout=300)
```

All tasks run concurrently. Each gets its own forked session with full parent
context. Prompt cache applies across siblings (~90% discount after the first).

Alternatively, make multiple `invoke` calls in the same response for
individual UI tracking (each shows its own spinner and completion). Note:
multiple separate `invoke` calls also run concurrently since the MCP server
is async, but `invoke_parallel` uses a single callstack.py process with
ThreadPoolExecutor which has lower overhead.

### Interactive mode — CALL/YIELD/RETURN protocol

The CALL/YIELD/RETURN protocol is always active. Any task can CALL children,
YIELD for user input, or RETURN a result — including tasks within parallel
mode.

Output is always JSON. On completion:
```json
{"status": "complete", "result": "...", "duration": 12.3}
```

On yield (needs user input):
```json
{"status": "yield", "question": "Enter the MFA code", "session_id": "abc-123", ...}
```

Resume with the user's answer using the `invoke_resume` MCP tool:

```
invoke_resume(resume_session="abc-123", user_reply="847291", timeout=300)
```

### Session discovery (automatic)

The session ID is discovered automatically. Priority:
`session_id` param → `CALLSTACK_PARENT_SESSION` env → `CLAUDE_SESSION_ID` env → mtime heuristic.

### Parameters

**invoke** tool:

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `task` | Yes | — | The task to execute |
| `timeout` | No | `300` | Max seconds before kill |
| `session_id` | No | auto-discover | Override session UUID or file path |
| `model` | No | inherited | Model override (e.g. `sonnet`, `opus`) |
| `cwd` | No | current dir | Working directory |

**invoke_parallel** tool:

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `tasks` | Yes | — | List of tasks to run concurrently |
| `timeout` | No | `300` | Max seconds per agent before kill |
| `session_id` | No | auto-discover | Override session UUID or file path |
| `model` | No | inherited | Model override (e.g. `sonnet`, `opus`) |
| `cwd` | No | current dir | Working directory |

**invoke_resume** tool:

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `resume_session` | Yes | — | Session ID from a yielded call |
| `user_reply` | Yes | — | The user's reply to the yield question |
| `timeout` | No | `300` | Max seconds before kill |
| `cwd` | No | current dir | Working directory |

## How it works

1. **Session Discovery** — finds the active Claude Code session file on disk
2. **Session Fork** — `--fork-session` creates an independent copy (full context inheritance)
3. **Invocation** — spawns `claude --resume <fork-id> --output-format stream-json` with the task
4. **Permission Control** — tool permission requests are intercepted via the NDJSON control protocol
5. **Execution** — the forked session runs with your FULL context
6. **Return** — the result is returned as JSON to stdout

## The three control instructions

Forked sessions have three instructions for communicating with the runtime:

**CALL** — hand off work to a child. The child inherits full context.
**YIELD** — pause for user input.
**RETURN** — return result to caller.

The runtime manages an **execution tree**. When a forked session outputs CALL,
a child node is added and a new session is spawned. When the child completes,
the parent is resumed with the result. In parallel mode (`--tasks`), each
branch independently supports CALL/YIELD/RETURN — a branch can delegate to
sub-agents or pause for user input without blocking siblings.

## Critical rules

- **Do NOT summarize your context** — the forked session already has everything
- **DO be specific about what you want** — the task is the only new information
- **Forked sessions can themselves call** (up to max depth of 5)

## Audit trail

Each invocation saves a trace entry in `call_traces/` for debugging and
post-mortem analysis.
