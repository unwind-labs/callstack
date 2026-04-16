# agent-callstack

**A call stack for AI agents to run deeply nested workflows reliably. Includes automatic stack-based context compaction.**

AI agents today have two options for complex work. 

- Sub-agents start blind — no parent context, so they need to receive all necessary context from the main agent. This wastes tokens
- Main agent does everything itself — context accumulates, quality degrades, and lossy compaction throws away information the agent needed.

Neither method can handle deeply nested workflows (common in Enterprise systems) because the LLM has to maintain the call stack in-context, and it loses track.

agent-callstack gives your agents a proper call stack. Each `/call` forks the parent's full session context — like `fork()` in Unix. The child session inherits everything. When it completes, only its return value and compact execution report flows back. The parent's context stays clean.

For instance "implement app.." (A) needs to "implement auth module" (B), which in turn need to "write JWT middleware" (C). Without callstack, the agent would perform all operations in a growing context like [SAAAAAABBBBBBBBBBBBCCCBBBBBAAAAAAAAAAAAAAAA], resulting in context rot and forced lossy context compactions along the way.

What if the tasks only returned their results so the caller's context remains compact? That's what agent-callstack does.

```
Agent "implement app.."
│    context: [S░░░░░░░░░░░░░░░░░░░░░░]   ← system prompt S
│    (does some work)
│    context: [SAAAAAA░░░░░░░░░░░░░░░░]   ← accumulates A activities
│    /call "implement auth module"            ← forked session
│    │    context: [SAAAAAA░░░░░░░░░░░░░░░░]  ← inherits S+A
│    │    (does some work)
│    │    context: [SAAAAAABBBBBBBB░░░░]      ← accumulates B activities
│    │    /call "write JWT middleware"             ← forked session
│    │    │    context: [SAAAAAABBBBBBBB░░░░]      ← inherits S+A+B
│    │    │    (does some work)
│    │    │    context: [SAAAAAABBBBBBBBCCC░]      ← accumulates C activities
│    │    │    return {result + compacted CCC} = c ← return result c
│    │    │                                        ← exit and delete forked session
│    │    context: [SAAAAAABBBBBBBBc░░░░]     ← c added to context instead of CCC
│    │    (does some work)
│    │    context: [SAAAAAABBBBBBBBcBB░░]     ← accumulates more B activities
│    │    returns {result + compacted BBBBBBBBcBB} = b  ← return result b
│    │
│    context: [SAAAAAAb░░░░░░░░░░░░░░░░]  ← b added to context instead of BBBBBBBBcBB
│    (does some work)                     ← continues with clean context
```

Note that forked sessions share the parent's exact token prefix, so prompt cache (~90% cheaper token pricing) applies automatically.

## Install

```bash
git clone https://github.com/amolk/agent-callstack

# Copy the /call skill into your .claude
cp -r agent-callstack/.claude/skills/call ~/.claude/skills/
```

## Quick Start

Once installed, Claude Code can use `/call` directly.

```
You: "/call Implement the auth module, then /call write tests for it"

Claude: I'll handle this in two calls to keep my context clean.

[call - invoke (MCP): task="Implement JWT auth in src/auth.py"]
→ {"status": "complete", "result": "Created src/auth.py with login/logout/refresh endpoints..."}

[call - invoke (MCP): task="Write tests for src/auth.py"]
→ {"status": "complete", "result": "Created tests/test_auth.py, 12 tests, all passing..."}
```

Each forked session sees the full conversation so far (knew what patterns you discussed, what files exist, what your preferences are) but its intermediate work — the 50 tool calls, the failed attempts, the debugging — never entered the parent's context.

This is unlike subagents that do not see the full conversation context so far and so all context needs to be explicitly generated and passed in. This is suitable for independent tasks, but most workflow steps benefit from having session context.

## Example: Customer Support Refund

The `examples/customer_support/` directory demonstrates a complete workflow — customer authentication with MFA, order lookup, and refund processing — using skills and MCP tools.

```bash
cd examples/customer_support
claude
# Say: "Process a refund"
# Say: cust_7829 ord_91847 sarah.chen@example.com +15550142 did not like product
# Say: 847291
```

### What happens

```
Orchestrator (interactive session with user)
  │
  ├─ /call authenticate-customer           16.6s
  │    ├── verify_customer_identity tool      (inline)
  │    ├── get_customer tool                  (inline)
  │    ├── send_mfa_code tool                 (inline)
  │    └── op: yield   "Enter MFA code"       (ask user)
  │         user: "847291"
  │    └── validate_mfa_code tool             (inline)
  │    └── op: return  "Authenticated, session created"
  │
  ├─ /call lookup-order                     27.0s
  │    └── op: return  "2 items eligible, within return window"
  │
  └─ /call process-refund                    9.2s + 24.6s
       └── op: yield   "Item condition? (unopened/opened/damaged)"
            user: "damaged"
       └── calculate_refund, process_payment, send_email
       └── op: return  "Refund $82.48, txn_ref_88291"
```

## Example: Parallel Calls with Nested Forks

The `examples/parallel_calls/` directory demonstrates parallel fan-out with nested parallelism — the orchestrator calls three agents simultaneously, and one of those agents itself forks into two parallel sub-agents.

```bash
cd examples/parallel_calls
claude
# Say: "run"
```

### What happens

```
A (orchestrator)
├── B  (weather report — leaf)          ──┐
├── C  (market brief — fork node)       ──┤ parallel
│   ├── E  (exchange rates — leaf)  ──┐   │
│   └── F  (news headlines — leaf)  ──┘   │ nested parallel inside C
└── D  (stock report — leaf)            ──┘

A calls {B, C, D} in parallel via --tasks
  B calls get_weather("Tokyo"), get_weather("London"), returns
  C calls {E, F} in parallel via --tasks (nested fork)
    E calls get_exchange_rate("JPY"), get_exchange_rate("GBP"), returns
    F calls get_news_headline("tech"), get_news_headline("finance"), returns
  C combines E+F results, returns
  D calls get_stock_price("AAPL"), get_stock_price("GOOGL"), returns
A validates all 6 expected values: PASS
```

Each parallel branch independently supports the full `call`/`yield`/`return` protocol — a branch can delegate further, pause for user input, or return, without blocking its siblings.

## How It Works

Claude Code stores each conversation as a JSONL session file on disk.

### Session fork

When you `/call`, `callstack.py` discovers the parent's session file (via `CLAUDE_SESSION_ID` env var or by finding the most recently modified session JSONL) and spawns a forked child:

```
claude --resume <session-id> --fork-session \
       --output-format stream-json --input-format stream-json \
       --permission-prompt-tool stdio
```

`--fork-session` creates an independent copy of the session — the child wakes up with the parent's full message history plus the new task appended. It doesn't know it's a fork. It just continues the conversation.

### Bidirectional JSON protocol

The parent and child communicate over stdin/stdout using NDJSON (newline-delimited JSON). This enables two critical capabilities:

**Permission control** — When the child requests permission to use a tool (Bash, file writes, etc.), the request arrives as a structured message:

```json
{"type": "control_request", "request_id": "req_...", "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {...}}}
```

The runtime intercepts this and responds programmatically — no human in the loop for forked sessions:

```json
{"type": "control_response", "response": {"subtype": "success", "request_id": "req_...", "response": {"behavior": "allow", "updatedInput": {...}}}}
```

**User input (yield)** — When a child needs information only the user can provide (e.g., an MFA code), it emits a `{"op": "yield", "question": "..."}` envelope in a fenced ` ```json ` block as its final output. The runtime serializes the execution tree to a `.call_tree` sidecar file, exits, and returns the question as JSON:

```json
{"status": "yield", "question": "Enter the 6-digit MFA code", "session_id": "abc-123"}
```

The parent asks the user, then calls `invoke_resume(resume_session="abc-123", user_reply="847291")`. The runtime reloads the tree from disk and continues from exactly where it paused.

### Three control operations

The child runs, does its work, and emits exactly one JSON envelope wrapped in a fenced ` ```json ` code block as its final output. The `op` field selects one of three operations:

- `{"op": "return", "result": ..., "summary": ..., "next": ...}` — done. The runtime captures the result, saves a trace to `call_traces/`, and hands the compact result back to the parent as JSON. `summary` and `next` are optional.
- `{"op": "call", "task": "..."}` — the child wants to delegate further. The runtime adds a child node to the execution tree and forks again. Same mechanism, one level deeper (up to depth 5).
- `{"op": "yield", "question": "..."}` — needs user input. The tree is persisted to disk so the session can be resumed later.

### Execution tree

The runtime manages an **execution tree** rather than a linear stack. Each node tracks its status (pending, running, complete, yielded, error), its children, and its result. For parallel tasks (`invoke_parallel`), sibling nodes run concurrently via `ThreadPoolExecutor`, each with its own recursive execution loop. When all siblings complete (or yield), results are collected and returned. This naturally supports nested parallelism — a branch can itself fan out into parallel sub-branches.

## Credits

Concept and implementation by [Amol Kelkar](https://github.com/amolk). The core idea — function-call semantics for LLM agent orchestration (full context inheritance + automatic compaction on return) — was first designed and implemented in [Playbooks AI](https://runplaybooks.ai) (2023-2026). agent-callstack generalizes this to work with any agent harness, starting with Claude Code.
