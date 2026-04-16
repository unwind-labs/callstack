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

Once installed, Claude Code can use `/call` directly:

```
You: "Implement the auth module, then write tests for it"

Claude: I'll handle this in two calls to keep my context clean.

[Bash: python3 .claude/skills/call/callstack.py --task "Implement JWT auth in src/auth.py"]
→ returns: "Created src/auth.py with login/logout/refresh endpoints..."

[Bash: python3 .claude/skills/call/callstack.py --task "Write tests for src/auth.py"]
→ returns: "Created tests/test_auth.py, 12 tests, all passing..."
```

Each forked session sees the full conversation so far (knew what patterns you discussed, what files exist, what your preferences are) but its intermediate work — the 50 tool calls, the failed attempts, the debugging — never entered the parent's context.

This is unlike subagents that do not see the full conversation context so far and so all context needs to be explicitly generated and passed in. This is suitable for independent tasks, but many workflow steps benefit from having fuller context.

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
  │    └── ---YIELD--- "Enter MFA code"       (ask user)
  │         user: "847291"
  │    └── validate_mfa_code tool             (inline)
  │    └── ---RETURN--- "Authenticated, session created"
  │
  ├─ /call lookup-order                     27.0s
  │    └── ---RETURN--- "2 items eligible, within return window"
  │
  └─ /call process-refund                    9.2s + 24.6s
       └── ---YIELD--- "Item condition? (unopened/opened/damaged)"
            user: "damaged"
       └── calculate_refund, process_payment, send_email
       └── ---RETURN--- "Refund $82.48, txn_ref_88291"
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

Each parallel branch independently supports the full CALL/YIELD/RETURN protocol — a branch can delegate further, pause for user input, or return, without blocking its siblings.

See [docs/2026-04-15-execution-tree.md](docs/2026-04-15-execution-tree.md) for the design of the execution tree that enables this.

## How It Works

Claude Code stores each conversation as a JSONL session file on disk. That file *is* the context. agent-callstack exploits this directly.

When you `/call`, `callstack.py` finds the parent's session file (a `PreToolUse` hook writes PID→session mappings to facilitate this), copies it to a new clone-id UUID, and spawns `claude --resume <clone-id> --print <task>`. The child Claude Code process wakes up with the parent's full message history plus the new task appended. It doesn't know it's a fork. It just continues the conversation.

The child runs, does its work, and outputs one of three markers:

- `---RETURN---` — done. `callstack.py` captures the result, deletes the clone, saves a delta JSONL to `call_traces/` for forensics, and hands the compact result back to the parent.
- `---CALL---` — the child wants to delegate further. The runtime adds a child node to the execution tree and forks again from this child's session. Same mechanism, one level deeper.
- `---YIELD---` — the child needs user input (e.g., an MFA code). `callstack.py` serializes the entire execution tree to a `.call_tree` sidecar file, exits, and returns the question to the parent. When the user answers, `--resume-session` reloads the tree and continues from exactly where it paused.

The runtime manages an **execution tree** rather than a linear stack. Each node tracks its status (pending, running, complete, yielded, error), its children, and its result. For parallel tasks (`--tasks`), sibling nodes run concurrently via threads, each with its own recursive execution loop. When all siblings complete (or yield), results are collected and returned. This naturally supports nested parallelism — a branch can itself fan out into parallel sub-branches.

## Credits

Concept and implementation by [Amol Kelkar](https://github.com/amolk). The core idea — function-call semantics for LLM agent orchestration (full context inheritance + automatic compaction on return) — was first designed and implemented in [Playbooks AI](https://runplaybooks.ai) (2023-2026). agent-callstack generalizes this to work with any agent harness, starting with Claude Code.
