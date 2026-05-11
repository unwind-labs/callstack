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

Use the `call` MCP tool. It always takes an array of tasks — pass one entry for a single task, multiple entries for concurrent execution. Each task gets the parent's full context.

```
call(tasks=["Implement and test the authentication module using the patterns we discussed"], timeout=300)
```

```
call(tasks=["Apply fix to A", "Apply fix to B"], timeout=300)
```

Don't pass extra context, just write the task command. Each task will have full context of the caller.

### Modes

The `context` parameter controls how each task's underlying claude session is launched:

- `context="fork"` (default) — child inherits the parent's full conversation via `--resume + --fork-session`. Use for "delegate a follow-up step the child already understands."
- `context="fresh"` — brand-new isolated session. Only the task string crosses the boundary. Same semantics as Claude Code's built-in `Agent` / `Task` tool. Use when you want an independent worker that shouldn't see the parent transcript.

```
call(tasks=["Audit src/auth for OWASP top-10 issues"], context="fresh")
```

In `fresh` mode you DO need to include any context the child needs **inside the task string** — the parent conversation is not inherited.

### Cross-project calls

Pass `cwd` with `{PWD}` substitution to run the child in a different project folder. Only valid with `context="fresh"` (forking into another project would produce a session with a transcript tied to project A but a cwd of project B — confusing and error-prone, so it's rejected with an explicit error).

```
call(tasks=["List the top-level files and summarize the README"],
     context="fresh", cwd="{PWD}/../sibling-repo")
```

`{PWD}` substitutes to the caller's project folder. The child's report still grafts under the caller's `report.yaml`, so the call tree stays unified.

### Response format

`call` returns `{invoke_id, report_path, results: [...]}`. Each entry in `results` is one of 3 response types:

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

Resume with the user's answer using the `resume` MCP tool:

```
resume(resume_session="abc-123", user_reply="847291", timeout=300)
```

## Critical rules

- **Do NOT send your context in `fork` mode** — the forked session already has all your context, so just say what task in 1 line. In `fresh` mode, do include any context the child needs in the task string.
- **Forked sessions can themselves call** (up to max depth of 5)
- **`fork` + different `cwd` is rejected** — combining `context="fork"` with a `cwd` that resolves to a different project folder returns an error. Use `context="fresh"` for cross-project work.
