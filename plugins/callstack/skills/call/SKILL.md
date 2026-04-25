---
name: call
description: "Executes given tasks in `claude --fork-session` instances. Caller gets back execution summary and results, keeping caller's context clean."
when_to_use: "Instead of using a subagent; user says 'call', 'fork', or 'delegate'."
---

# /call — Execute in forked session

You must use `/call` for any task that is best offloaded to a subagent. You get back compact execution summary, results and link to full log so your context remains clean. Each call runs in a forked session so it has all your context, so you don't need to send in context, send just the task. You can execute multiple tasks in parallel like `/call Apply fix to a.py and b.py in parallel`. 

## When to use

- Any task that needs a subagent (use instead of using a subagent)
- Any task that has nested subtasks, e.g. implement a widget
- Any task that may take more than a minute to execute
- Any task that has recursive steps, e.g. monte carlo tree search should use nested calls, so discarded braches do not pollute context

## When NOT to use

- Single, trivial task
- Single shell commands
- Trivially short tasks where the overhead (about 5 seconds) isn't worth it
- Incremental, interactive sequence of tasks

## Invocation

### Single mode

For a single task, use the `invoke` MCP tool:

```
invoke(task="Implement and test the authentication module using the patterns we discussed", timeout=300)
```

### Parallel mode — multiple independent tasks

Use `invoke_parallel` MCP tool for concurrent execution:

```
invoke_parallel(tasks=["Apply fix to A", "Apply fix to B"], timeout=300)
```

### Response format

Each /call task returns a JSON response. There are 3 types of responses -

#### Completion
```json
{
  "status": "complete",
  "result": "...",
  "summary": "Compacted brain-dump of what happened — decisions, sub-calls, side effects",
  "suggested_next": "Run the test suite to verify",
  "duration": 12.3,
  "session_log": "/path/to/session.jsonl",
  "session_log_start_line": 42
}
```

- `result` — the deliverable/answer.
- `summary` — compact brain-dump the parent can rely on instead of reading
  the child's session log: sub-calls made and outcomes, key decisions,
  assumptions, side effects, dead ends. Optimized for tokens. May be `null`
  when the child has nothing worth carrying beyond `result`.
- `suggested_next` — the child's advisory suggestion for what should happen
  next. Not binding — the parent has broader context and decides — but it
  aligns the child's summary toward what matters for the next step.
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
