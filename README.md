# callstack

https://github.com/user-attachments/assets/a9ee9935-99d4-4564-a604-c9f9889afeab

Call stacks let humans build complex software by **scoping complexity** and **scoping memory and variables**. No matter how deep execution goes, the code runs with the full context of the program, and the language runtime guarantees the call stack unwinds deterministically as functions return.

No equivalent capability exists for ReAct-loop based agents — and Claude Code is a ReAct-loop based agent harness. These agents accumulate context linearly, and as the conversation grows, important early details get crowded out and the agent loses track. Subagents are available to execute side tasks in a fresh context and return results back, keeping execution detail out of the main context — but a subagent must be sent every piece of context it needs, which wastes token generation. Recently, Claude Code shipped an experimental subagent mode called [`/fork`](https://docs.claude.com/en/docs/claude-code) where forked subagents run in a forked session and inherit the parent's context. But as of today, forked agents cannot themselves fork (no deep call stacks), and no user interaction is allowed inside a forked subagent — both limits constrain real use.

## Why are deep call stacks necessary?

Most real-world workflows are deep. Consider a customer-support refund: the orchestrator authenticates the customer (which itself fans out into identity verification, MFA challenge, MFA validation), looks up the order (lookup → eligibility check → return-window check), and processes the refund (condition assessment → calculation → payment → email). Each step may itself need to delegate further. Flatten the whole thing into one ReAct loop and the agent must hold every intermediate detail in context simultaneously.

LLMs are notoriously poor at maintaining a deep call chain and reliably unwinding as tasks finish. The longer the chain, the more likely the orchestrator forgets the original goal by the time control returns to it.

## What the callstack plugin delivers

- **`/call` simulates a function call.** Fork the parent's session, run the task, return a compact result.
- **Parallel forks.** Run multiple tasks simultaneously with one invocation: `/call do X, do Y, and do Z in parallel`. The user-facing surface stays `/call`; the skill picks single vs parallel internally.
- **Calls return the result and a path to the full session JSON.** If the caller later needs a detail the child didn't include, file access reaches the full transcript without burning more context.
- **Interactivity at any depth.** A `/call` can pause and ask the user from any frame — without paying the cost of bubbling the question through every intermediate node (which would otherwise require, and waste, an LLM turn at each level).
- **Calls run in the parent's full context** — so the invocation is one line, not a hand-rolled context dump.
- **Calls run in the parent's full context** — so the child understands what tasks will follow, and shapes its return payload to include what the caller will need next.
- **Fork or fresh, your choice.** `context="fork"` (default) inherits the parent transcript via `--fork-session`. `context="fresh"` launches an isolated session — same semantics as Claude Code's built-in `Agent`/`Task` tool, but with nested calls, yield/resume, and a merged report tree on top. Subagents become a strict subset of `/call`.
- **Cross-project calls.** Pass `cwd` (with `{PWD}` substitution for the caller's project) to run the child against a different repo. Useful for "look at the sibling repo and tell me X" without leaving the current session. Fork mode is rejected across projects (the transcript and cwd would disagree); fresh mode handles it cleanly.

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
│    │    │    return {result c}                   ← return result c
│    │    │                                        ← exit forked session
│    │    context: [SAAAAAABBBBBBBBc░░░░]     ← c added instead of CCC
│    │    (does some work)
│    │    context: [SAAAAAABBBBBBBBcBB░░]     ← accumulates more B activities
│    │    return {result b}
│    │
│    context: [SAAAAAAb░░░░░░░░░░░░░░░░]  ← b added instead of BBBBBBBBcBB
│    (does some work)                     ← continues with clean context
```

Forked sessions share the parent's exact token prefix, so prompt caching (~90% cheaper) applies automatically.

## Install

Two ways, pick one.

**Claude Code marketplace** (recommended):

```
/plugin marketplace add unwind-labs/callstack
/plugin install callstack@unwind-labs
```

**Manual** (clone the repo, drop the plugin into your Claude Code plugins directory):

```bash
git clone https://github.com/unwind-labs/callstack
cp -r callstack/plugins/callstack ~/.claude/plugins/
```

The plugin bundles the `/call` skill at `plugins/callstack/skills/call/SKILL.md`, the MCP server at `plugins/callstack/mcp_server.py`, and a SessionStart hook — all wired up automatically by Claude Code's plugin loader.

## Quick start

Once installed, Claude Code can use `/call` directly.

```
You: "/call Implement the auth module, then /call write tests for it"

Claude: I'll handle this in two calls to keep my context clean.

[call - call (MCP): tasks=["Implement JWT auth in src/auth.py"]]
→ {"results": [{"status": "complete", "result": "Created src/auth.py with login/logout/refresh endpoints..."}]}

[call - call (MCP): tasks=["Write tests for src/auth.py"]]
→ {"results": [{"status": "complete", "result": "Created tests/test_auth.py, 12 tests, all passing..."}]}
```

For independent tasks, ask for parallel execution and the skill fans them out in a single fork:

```
You: "/call profile the API, audit the deps, and benchmark the renderer in parallel"

Claude: Running all three concurrently.

[call - call (MCP): tasks=["Profile the API", "Audit the deps", "Benchmark the renderer"]]
→ {"results": [{"status": "complete", "result": "API: p99 184ms, hot path is..."},
              {"status": "complete", "result": "Deps: 3 outdated, 1 CVE in..."},
              {"status": "complete", "result": "Renderer: 47 fps median, dropped frames at..."}]}
```

Each forked session sees the full conversation so far (knew what patterns you discussed, what files exist, what your preferences are) but its intermediate work — the 50 tool calls, the failed attempts, the debugging — never enters the parent's context.

This is unlike subagents which do not see the conversation context, so all context has to be hand-rolled and passed in. Subagents are right for genuinely independent tasks; most workflow steps benefit from inherited context.

### Fresh sessions and cross-project calls

When you do want subagent-style isolation — an independent worker that shouldn't see the parent transcript — pass `context="fresh"`:

```
You: "/call audit src/auth for OWASP top-10 issues in a fresh session"

[call - call (MCP): tasks=["Audit src/auth for OWASP top-10 issues"], context="fresh"]
→ {"results": [{"status": "complete", "result": "3 findings: ..."}]}
```

Fresh-mode children still emit `call`/`yield`/`return` envelopes, so they can themselves nest further calls, pause for user input, and contribute frames to the same merged `report.yaml`. They just don't inherit the parent's conversation — include any needed context in the task string.

To run a child against a different repo, pass `cwd`. `{PWD}` resolves to the caller's project folder:

```
[call - call (MCP):
   tasks=["List top-level files and summarize the README"],
   context="fresh",
   cwd="{PWD}/../sibling-repo"]
```

`context="fork"` combined with a `cwd` in a different project is rejected — the forked transcript would be tied to project A while the cwd points at project B, which is incoherent. Cross-project work must be `fresh`.

## Example: customer support refund

The `examples/customer_support/` directory demonstrates a complete workflow — customer authentication with MFA, order lookup, and refund processing — using skills and MCP tools.

```bash
cd examples/customer_support
claude
# Say: "Process a refund for Sarah Chen (cust_7829), order ord_91847.
#       Email: sarah.chen@example.com, Phone: +1-555-0142."
# Say: 000000      (wrong on purpose — leaf re-asks)
# Say: 847291      (correct — leaf validates and unwinds)
# Say: damaged
```

### What happens

The MFA branch is **4 levels deep**. The leaf skill (`/check-code-expiry`) is the one that actually talks to the user — it yields for the code, validates it, and retries once if wrong. Each yield surfaces all the way up to the root orchestrator (which owns the user's terminal); each resume re-enters at the leaf with the reply already in scope.

```
Orchestrator (root — owns the user's terminal)
│
├─ /call authenticate-customer                                       ← depth 1
│   ├── verify_customer_identity tool
│   ├── get_customer tool
│   └─ /call verify-mfa                                              ← depth 2
│       ├── send_mfa_code tool
│       └─ /call validate-mfa-code                                   ← depth 3
│           └─ /call check-code-expiry                               ← depth 4 (leaf)
│               │
│               ├── op: yield "Enter the 6-digit MFA code" ─────────┐  yield #1
│               .                                                   │  surfaces 4 → root
│               .   (whole subtree snapshotted to .call_tree;       │
│               .    no intermediate frame wakes)                   │
│               .                                                   ▼
│ ╔═══════════════════════════════════════════════════════════════════════╗
│ ║ ROOT  prompt → user :  "Enter the 6-digit MFA code"                   ║
│ ║ ROOT  user → prompt :   000000           (wrong on purpose)           ║
│ ╚═══════════════════════════════════════════════════════════════════════╝
│               .                                                   │
│               .   resume root → 4, leaf re-enters mid-procedure   │
│               ├── resume(user_reply="000000")  ◀──────────────────┘
│               ├── validate_mfa_code tool        → invalid
│               │
│               ├── op: yield "That code was incorrect. Re-enter." ─┐  yield #2
│               .                                                   │  surfaces 4 → root
│               .                                                   ▼
│ ╔═══════════════════════════════════════════════════════════════════════╗
│ ║ ROOT  prompt → user :  "That code was incorrect. Please re-enter."    ║
│ ║ ROOT  user → prompt :   847291           (correct)                    ║
│ ╚═══════════════════════════════════════════════════════════════════════╝
│               .                                                   │
│               .   resume root → 4                                 │
│               ├── resume(user_reply="847291")  ◀──────────────────┘
│               ├── validate_mfa_code tool        → valid
│               └── op: return  "MFA validated (2 attempts)"  ──── unwind 4 → 3
│           └── op: return  "MFA code accepted"               ──── unwind 3 → 2
│       └── op: return  "MFA verified"                        ──── unwind 2 → 1
│   └── op: return  "Authenticated, session sess_…"           ──── unwind 1 → root
│
├─ /call lookup-order                                                ← depth 1
│   └── op: return  "2 items eligible, within return window"   ──── unwind 1 → root
│
└─ /call process-refund                                              ← depth 1
    │
    ├── op: yield "Item condition? (unopened/opened/damaged)" ──────┐  yield #3
    .                                                               │  surfaces 1 → root
    .                                                               ▼
  ╔═══════════════════════════════════════════════════════════════════════╗
  ║ ROOT  prompt → user :  "Item condition? (unopened/opened/damaged)"    ║
  ║ ROOT  user → prompt :   damaged                                       ║
  ╚═══════════════════════════════════════════════════════════════════════╝
    .                                                               │
    .   resume root → 1                                             │
    ├── resume(user_reply="damaged")  ◀─────────────────────────────┘
    ├── calculate_refund, process_payment, send_email
    └── op: return  "Refund $82.48, txn_ref_88291"               ──── unwind 1 → root
```

Each double-bordered box is the **user's actual terminal**: a prompt printed by the ROOT frame and a reply typed back into the ROOT frame. The user never sees a frame at depth 2, 3, or 4 — those frames are paused on disk while the user is typing. The arrows trace the round-trip: yield surfaces from the originating depth straight to root (skipping every intermediate frame, no LLM turn burned to "relay" the question), and resume re-enters at the exact same depth with the reply in scope.

Notice yields #1 and #2 both originate at depth 4. After the wrong code, depth 4 picks up at the next line of its own procedure (the "retry once" branch in [check-code-expiry/SKILL.md:24-34](examples/customer_support/.claude/skills/check-code-expiry/SKILL.md)) — depths 1, 2, 3 stay frozen the whole time. Only after `validate_mfa_code` finally succeeds does the leaf return, and the stack unwinds four frames in order (4 → 3 → 2 → 1 → root) before the orchestrator issues the next `/call lookup-order`.

## Example: parallel calls with nested forks

The `examples/parallel_calls/` directory demonstrates parallel fan-out with nested parallelism — the orchestrator calls three agents simultaneously, and one of those agents itself forks into two parallel sub-agents.

```bash
cd examples/parallel_calls
claude
# Say: "run"
```

### What happens

```
Orchestrator (root)
│
├── op: call tasks=[task-b, task-c, task-d]            ← parallel fan-out (3 siblings)
│
├─ /call task-b   weather report                                  ← depth 1, sibling 1/3
│   ├── get_weather("Tokyo") tool
│   ├── get_weather("London") tool
│   └── op: return  "Tokyo 18°C, London 11°C"                  ──── unwind 1 → root
│
├─ /call task-c   market brief                                    ← depth 1, sibling 2/3
│   │
│   ├── op: call tasks=[task-e, task-f]          ← nested parallel fan-out (2 siblings)
│   │
│   ├─ /call task-e   exchange rates                             ← depth 2, sibling 1/2
│   │   │
│   │   ├── op: call tasks=[task-g, task-h]    ← nested parallel fan-out (2 siblings)
│   │   │
│   │   ├─ /call task-g   JPY rate                              ← depth 3, sibling 1/2
│   │   │   ├── get_exchange_rate("JPY") tool
│   │   │   └── op: return  "JPY 152.3"                      ──── unwind 3 → 2
│   │   │
│   │   └─ /call task-h   GBP rate                              ← depth 3, sibling 2/2
│   │       ├── get_exchange_rate("GBP") tool
│   │       └── op: return  "GBP 0.79"                       ──── unwind 3 → 2
│   │   │
│   │   └── op: return  "JPY=152.3, GBP=0.79"                ──── unwind 2 → 1
│   │
│   └─ /call task-f   news headlines                             ← depth 2, sibling 2/2
│       ├── get_news_headline("tech") tool
│       ├── get_news_headline("finance") tool
│       └── op: return  "tech: …, finance: …"                 ──── unwind 2 → 1
│   │
│   └── op: return  "rates + headlines combined"               ──── unwind 1 → root
│
└─ /call task-d   stock report                                    ← depth 1, sibling 3/3
    ├── get_stock_price("AAPL") tool
    ├── get_stock_price("GOOGL") tool
    └── op: return  "AAPL $189.4, GOOGL $142.1"                ──── unwind 1 → root

Orchestrator validates all 6 expected values across the three returned summaries: PASS
```

Every `op: call tasks=[…]` with multiple entries is a **parallel fan-out** — the runtime spawns one forked subprocess per sibling and runs them concurrently, bounded only by `CALLSTACK_MAX_FANOUT` (default 64) at the MCP boundary. Each sibling independently supports the full `call`/`yield`/`return` protocol, so a parallel branch can itself fan out (depth 2 inside `task-c`), pause for user input, or return — without blocking its siblings. The 3-level deep nesting under `task-c` (orchestrator → c → e → {g, h}) shows that parallel batches compose: a sibling at any depth can become the parent of its own parallel batch.

> **No global cap on live `claude` processes.** Each forked child runs its own `agent_callstack` runtime; `CALLSTACK_MAX_FANOUT` bounds per-call fan-out but does not cap total live processes across a deep tree. A tree that is both **wide and deep** can hold many `claude` subprocesses live at once, each ~0.5–2 GB RSS. Use `CALLSTACK_MAX_DEPTH` to bound depth (see [Configuration](#configuration)); a tree-wide live-process cap is a deferred design (`dev/RFC-harvest-on-demand.md`).

Each `task-*` node is a Claude Code Skill at `examples/parallel_calls/.claude/skills/task-*/SKILL.md`. `/call` invokes them by name, and any Skill is free to itself `/call` other Skills — Skills become the "functions" in the call stack: small, named, composable units the orchestrator wires together.

## How it works

Claude Code stores each conversation as a JSONL session file on disk.

### Session fork (default) and fresh sessions

When you `/call` with the default `context="fork"`, the runtime (the `agent_callstack` package) discovers the parent's session file (via `CLAUDE_SESSION_ID` env var or by finding the most recently modified session JSONL in the caller's project) and spawns a forked child:

```
claude --resume <session-id> --fork-session --session-id <uuid> \
       --output-format stream-json --input-format stream-json \
       --permission-prompt-tool stdio
```

`--fork-session` creates an independent copy of the session — the child wakes up with the parent's full message history plus the new task appended. It doesn't know it's a fork. It just continues the conversation.

`--session-id <uuid>` pins the forked child's own session id to a UUID the runtime preallocates, instead of letting Claude Code assign one. That id is also stamped into the child's environment (`CALLSTACK_OWN_SESSION`), so the child's MCP server can identify its own session deterministically rather than guessing by JSONL mtime; a hard guard refuses to continue if the two disagree.

With `context="fresh"` the runtime drops `--resume` and `--fork-session`, so `claude` starts a brand-new session. Only the task string crosses the boundary. The same NDJSON protocol still drives it, so fresh children retain `call`/`yield`/`return` semantics, depth, and report grafting — just without inherited context. Nested calls *inside* a fresh child still fork from that child's session (a fresh root can grow a normal sub-tree of forks beneath it).

When `cwd` is passed and resolves to a different project than the caller's, the call is tagged `fresh_cross_project` — the runtime locates the parent session in the caller's project (not the child's redirected cwd) and propagates that identity into the merged report.

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

The parent asks the user, then calls `resume(resume_session="abc-123", user_reply="847291")`. The runtime reloads the tree from disk and continues from exactly where it paused.

### Three control operations

The child runs, does its work, and emits exactly one JSON envelope wrapped in a fenced ` ```json ` code block as its final output. The `op` field selects one of three operations:

- `{"op": "return", "result": ..., "summary": ..., "next": ...}` — done. The runtime captures the result, appends a trace line to the invocation's `call_trace.jsonl`, and hands the compact result back to the parent as JSON. `summary` and `next` are optional.
- `{"op": "call", "task": "..."}` — the child wants to delegate further. The runtime adds a child node to the execution tree and forks again. Same mechanism, one level deeper (default depth 10, widenable via `CALLSTACK_MAX_DEPTH` up to a hard ceiling of 32).
- `{"op": "yield", "question": "..."}` — needs user input. The tree is persisted to disk so the session can be resumed later.

### Execution tree

The runtime manages an **execution tree** rather than a linear stack. Each node holds an immutable state value (`Pending`, `AwaitingTurn`, `AwaitingChild`, `AwaitingUser`, `Done`, `Failed`); transitions are computed by a pure `step(state, event) -> (new_state, [effects])` function in `agent_callstack.state`. The driver in `agent_callstack.driver` performs the effects (subprocess turns, child spawns) and feeds the resulting events back. When `call` receives multiple tasks, sibling root nodes run concurrently via `ThreadPoolExecutor`. When a node yields, the entire subtree is snapshotted to a `.call_tree` sidecar; resume reloads it and re-enters the loop with `UserReplied`.

### Package layout

```
plugins/callstack/agent_callstack/
  __init__.py        Public API: call, call_many, resume, Caller, Result
  state.py           Pure state machine: discriminated unions + step()
  driver.py          Effect runner: ties channel + state + tree together
  channel.py         Claude CLI subprocess + NDJSON protocol (the live subprocess seam)
  testing.py         ScriptedChannel: in-memory channel seam for tests
  protocol.py        SYSTEM_INSTRUCTION + envelope parser
  invocation.py      InvocationFactory: root-vs-nested identity decision
  invocation_ctx.py  Per-invocation context (ids, paths, env snapshot)
  session.py         SessionLocator: ~/.claude/projects discovery
  background.py      run_in_background registry + reconcile/reap lifecycle
  shutdown.py        Signal-handler chaining + active-reporter flushing
  terminal_wait.py   Pre-seal wait for late return/yield; timeout expiry
  env.py             Typed readers for all CALLSTACK_* env knobs
  frames.py          On-disk frame reconciliation + merged-report grafting
  trace.py           TraceWriter (JSONL) + TreeStore (sidecar snapshots)
  report.py          InvocationReport facade over the report.yaml
  reporter.py        LiveReporter: debounced, lock-serialized YAML merge
  results.py         Terminal-node → Result/CallFailed translation
  analysis.py        SessionAnalyzer: post-execution structured inspection
```

`channel.py` is the live subprocess seam; `testing.py` provides a scripted in-memory channel so the driver and state machine can be tested without spawning `claude`.

## How is `/call` better than `/fork`?

Both `/call` and Anthropic's experimental `/fork` are built on the same underlying CLI primitive — `claude --session-id <uuid> --fork-session`, which copies a named session and resumes the forked copy with the parent's full context. They are sibling runtimes on that primitive, not parent/child. Here is how they compare:

| Capability   | Claude Code `/fork`                                        | callstack `/call`                                                      |
|--------------|------------------------------------------------------------|------------------------------------------------------------------------|
| Fork depth   | Single level — forks cannot fork                           | Arbitrary depth — full recursive call stack                            |
| Interactivity| Background only; result returns as a message               | Interactive at every level; user can drop into any frame               |
| Runtime mode | Interactive sessions only; disabled in non-interactive use | Works headless via `claude -p`                                         |
| Observability| None — fork runs in a side panel                           | [unwind](https://pypi.org/project/unwind-labs/): live web UI of the call tree across all sessions |
| Concurrency  | Implicit                                                   | Fan-out bounded by `CALLSTACK_MAX_FANOUT` at the MCP boundary           |

**Depth.** `/fork` is one level deep — a fork cannot itself fork. Real workflows nest: "implement app" calls "implement auth module" calls "write JWT middleware". `/call` is recursive (default cap 10 levels, widenable via `CALLSTACK_MAX_DEPTH` up to a hard ceiling of 32), so the whole tree runs as a proper call stack instead of being flattened into the parent.

**Interactivity at every level.** A `/fork` runs in the background and returns a message when done. A `/call` can pause mid-execution with `{"op": "yield", "question": "..."}` and ask the user — at any depth. Auth flows that need an MFA code, refund flows that need a damage assessment, deployments that need a confirmation: the user drops into the exact frame that needs them, then control returns to the stack.

**Headless.** `/fork` is explicitly disabled in non-interactive runs and the Agent SDK. `/call` uses the same `claude --resume … --fork-session` plumbing but drives it over stdin/stdout NDJSON, so it works headless via `claude -p`. Same primitive, no harness gate.

**Observability.** `/fork` runs in a side panel with no external view. unwind-labs ships [unwind](https://pypi.org/project/unwind-labs/), a Python web UI that tails `~/.claude/projects/*.jsonl` and the runtime's `call_trace.jsonl` files to render a live call tree across all sessions, with each frame's conversation expandable in a side pane.

**Fan-out, not bounded concurrency.** Each forked `claude` subprocess takes ~0.5–2 GB RSS. There is **no global cap on live `claude` processes** — a previous design (filesystem-token semaphore) was removed because sync-blocked parents held slots forever, which caused tree-wide deadlocks under depth. The only bound is per-`/call` fan-out via `CALLSTACK_MAX_FANOUT` (default 64). A future "harvest-on-demand" design (see `dev/RFC-harvest-on-demand.md`) may reintroduce a tree-wide live-process bound; for now, size your trees against your RAM budget.

`/fork` validated the primitive. `/call` ships the call stack.

## Configuration

### All environment knobs

Every knob is read through a typed, clamped reader in [plugins/callstack/agent_callstack/env.py](plugins/callstack/agent_callstack/env.py); an unset, non-numeric, or out-of-range value falls back to the default.

| Env var | Default | Clamp / ceiling | Purpose |
|---------|---------|-----------------|---------|
| `CALLSTACK_MAX_DEPTH` | `10` | hard ceiling `32` | Max recursion depth of the call tree. Stamped onto every child so the budget is inherited. |
| `CALLSTACK_MAX_FANOUT` | `64` | must be `> 0` | Max `len(tasks)` accepted in one `call` at the MCP boundary (DoS guard — each task forks a `claude` process). |
| `CALLSTACK_MAX_BACKGROUND` | `64` | must be `> 0` | Max `run_in_background=True` invocations parked awaiting `await_call`. Hitting it is a loud error, not a silent eviction. |
| `CALLSTACK_REPORT_DEBOUNCE_SECS` | `0.25` | `>= 0` | Debounce window before the live reporter flushes a merged `report.yaml`. `0` = synchronous merge. |
| `CALLSTACK_FINALIZE_WAIT_SECONDS` | `120` | `[0, 600]` | How long the runtime blocks waiting for late `return`/`yield` envelopes before sealing the report. `0` = seal immediately. |
| `CALLSTACK_ORPHAN_TTL_SECONDS` | `1200` | `[0, 86400]` | Wall-clock age past which a frame's writer is treated as abandoned regardless of `os.kill(pid, 0)` (PID-reuse defense). `0` = rely on liveness probe alone. |

## Credits

Concept and implementation by [Amol Kelkar](https://github.com/amolk). The core idea — function-call semantics for LLM agent orchestration (full context inheritance, compact return) — was first designed and implemented in [Playbooks AI](https://runplaybooks.ai) (2023–2026). The `callstack` plugin generalizes it to any agent harness, starting with Claude Code.
