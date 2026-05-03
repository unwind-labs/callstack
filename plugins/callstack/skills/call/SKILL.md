---
name: call
description: "Executes given tasks in `claude --fork-session` instances. The forked session has caller's full context including decision to perform the task(s), so only indicate what task the fork should execute. Caller gets back execution summary and results, keeping caller's context clean."
when_to_use: "Must use for any non-trivical task or a TODO item; user says 'call', 'fork', or 'delegate'."
---

# /call — Execute in forked session

When you execute a task as a call, you get back compact execution summary, results and link to full log so your context remains clean. The forked session has caller's full context including decision to perform the task(s), so only indicate what task the fork should execute - send a one line task. You can execute multiple tasks in parallel like `/call Apply fix to a.py and b.py in parallel`.

## When to use

- Any task that needs a subagent (use call instead of using a subagent)
- Any task that has nested subtasks, e.g. implementing a widget that may have sub tasks
- Any task that may take more than a minute, has many steps or is recursive to execute should use a /call so main agent's context remains clutter free

## Execution

### Single task

For a single task, use the `invoke` MCP tool:

```
invoke(task="Implement and test the authentication module using the patterns we discussed", timeout=300)
```

### Parallel tasks

Use `invoke_parallel` MCP tool for concurrent execution:

```
invoke_parallel(tasks=["Apply fix to A", "Apply fix to B"], timeout=300)
```

Don't pass extra context, just write the task command. Each task will have full context of the caller.

### Response format

Each /call task returns a JSON response. There are 3 types of responses -

#### Completion
```json
{
  "status": "complete",
  "result": "...",
  "suggested_next_task": "Next run the test suite",
  "summary": "Compacted brain-dump of what happened - decisions, sub-calls, side effects - ensuring information needed for next task",
  "duration": 12.3,
  "session_log": "/path/to/session.jsonl",
  "session_log_start_line": 42
}
```

- `result` — the deliverable/answer.
- `suggested_next_task` — the child's advisory suggestion for what should happen
  next. Not binding — the parent has broader context and decides — but it
  aligns the child's summary toward what matters for the next step.
- `summary` — compact brain-dump the parent can rely on instead of reading
  the child's session log: sub-calls made and outcomes, key decisions,
  assumptions, side effects, dead ends. Optimized for tokens. May be `null`
  when the child has nothing worth carrying beyond `result`.
- `session_log` — file path to the forked session's JSONL log.
- `session_log_start_line` — 1-based line where the child's additions begin.
  The session log starts with `## Starting Task [<id>]` at this point.
  Lines before it are inherited parent context.

#### Yield
When the forked session needs a user input

```json
{"status": "yield", "question": "Enter the MFA code", "session_id": "abc-123", ...}
```

Resume with the user's answer using the `invoke_resume` MCP tool:

```
invoke_resume(resume_session="abc-123", user_reply="847291", timeout=300)
```

## Critical rules

- **Do NOT send your context** — the forked session already has all your context, so just say what task in 1 line
- **Forked sessions can themselves call** (up to max depth of 5)
