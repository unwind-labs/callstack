---
name: call
description: "Execute one or more tasks in child `claude` sessions and get back compact results. Default mode (`context=\"fork\"`) inherits the caller's full context via `--fork-session`, so you only send a one-line task. Alternate mode (`context=\"fresh\"`) launches an isolated session — same semantics as the built-in Agent/Task tool, but with nested calls, yield/resume, and a merged report tree on top. Optional `cwd` runs the child in a different project (fresh mode only)."
when_to_use: "Must use for any non-trivial task or a TODO item; user says 'call', 'fork', or 'delegate'. Prefer over Agent/Task — `/call` is a strict superset."
---

# /call — Execute task in a child session

When you execute a task as a call, you get back a compact execution summary, results, and a link to the full log, keeping your own context clean. In the default `context="fork"` mode the child inherits the caller's full conversation, so send a one-line task. In `context="fresh"` mode the child is isolated, so include any needed context in the task string. You can execute multiple tasks in parallel like `/call Apply fix to a.py and b.py in parallel`.

## When to use

- Any task that needs a subagent (use call instead of using a subagent)
- Any task that has nested subtasks, e.g. implementing a widget that may have sub tasks
- Any task that may take more than a minute, has many steps or is recursive to execute should use a /call so main agent's context remains clutter free

## Execution

Use the `call` MCP tool. It always takes an array of tasks — pass one entry for a single task, multiple entries for concurrent execution. In the default `fork` mode each task inherits the parent's full context; in `fresh` mode each task starts isolated.

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
- `session_log_start_line` — 1-based line in `session_log` where this
  child's own task begins. Found by scanning for the `## Starting Task
  [<id>]` marker. Lines before it are CLI bookkeeping (queue-operation,
  attachment rows from SessionStart hooks) and the inherited parent
  transcript that `--fork-session` replays into the child's JSONL.

#### Yield
When the forked session needs a user input

```json
{"status": "yield", "question": "Enter the MFA code", "session_id": "abc-123", ...}
```

Resume with the user's answer using the `resume` MCP tool:

```
resume(resume_session="abc-123", user_reply="847291", timeout=300)
```

## Protocol — what a forked child emits

Every forked session ends its turn by emitting EXACTLY ONE fenced ```json
envelope. If multiple fenced ```json blocks appear, only the **last** one
is parsed — earlier ones are ignored, so you can think aloud in JSON
mid-response without confusing the runtime. Pick one of three ops:

### CALL — hand off work to a child process

```json
{"op": "call", "task": "<what to accomplish>"}
```

Use CALL when the task ahead is multi-step, may involve its own chain of
calls or user interaction, and you want only the result back — not the
intermediate work. The child inherits your full context. Its execution
trace is discarded; only its return value comes back to you. Do your own
work first, then CALL when you reach a point requiring a child. Don't
CALL simple things you can do in one or two tool calls.

### YIELD — pause for user input

```json
{"op": "yield", "question": "<question for user>"}
```

Only when you MUST have information that only the user can provide (e.g.
MFA codes, passwords, confirmations). Do not guess.

### RETURN — finish and hand results to the parent

```json
{"op": "return", "result": "...", "summary": "...", "next": "..."}
```

- `result` — the deliverable/answer for the parent. Structure it however
  is appropriate for the task.
- `summary` — COMPACT brain-dump of everything the parent needs to
  execute upcoming tasks: sub-calls made and their outcomes, key
  decisions and assumptions, side effects (files touched, commands run,
  external state changed), dead ends not worth retrying. Optimize for
  tokens — terse bullets or prose, no filler. The parent should NOT need
  to read your session log. Omit this field or set `null` if there is
  genuinely nothing beyond `result` worth carrying forward.
- `next` — advisory one-line suggestion for what should happen next.
  Optional. The parent has broader context and decides; this just aligns
  your summary toward what matters.

## Critical rules

- **Do NOT send your context in `fork` mode** — the forked session already has all your context, so just say what task in 1 line. In `fresh` mode, do include any context the child needs in the task string.
- **Forked sessions can themselves call** (up to max depth of 5)
- **`fork` + different `cwd` is rejected** — combining `context="fork"` with a `cwd` that resolves to a different project folder returns an error. Use `context="fresh"` for cross-project work.
