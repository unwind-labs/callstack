# Nested Mixed Calls Example

This example demonstrates a topology that mixes the **Agent tool** (subagents) with
**`/call`** (forked and fresh contexts) at multiple depths.

## Call Tree

```
main (this agent — orchestrator)
├── Agent: S                       [parallel — named subagent from .claude/agents/S.md]
├── Agent: general-purpose         [parallel — short inline task, agent named "gp1"]
└── /call /task-a   (forked)       [sequential — runs after both subagents return]
    ├── /call /task-b   (fresh)    [parallel]
    ├── /call /task-c   (forked)   [parallel]
    │   ├── /call /task-e (forked)
    │   └── Agent: general-purpose   [runs AFTER E returns; agent named "F"]
    └── /call /task-d   (forked)   [parallel — leaf]
```

The interesting properties:

- The two top-level subagents launch via the **Agent tool**, not `/call`.
- `A` is invoked via `/call` with `context="fork"`, inheriting main's context.
- Inside `A`, the three parallel children mix `context="fresh"` (B) with `context="fork"` (C, D).
- `C` is itself a fork node: it first runs `/call /task-e` (forked), then — only after E
  returns — launches a general-purpose subagent `F`.
- `D` is a leaf and returns immediately.

## Workflow

When the user says **"run"**, execute the steps below.

### Step 1 — Fork: launch the two top-level subagents in parallel

In a **single message**, dispatch both subagents concurrently using the Agent tool:

1. `subagent_type: "S"` — no extra prompt context needed; the agent definition in
   `.claude/agents/S.md` carries the full instructions.
2. `subagent_type: "general-purpose"` with the prompt:
   > You are agent **gp1**. Return ONLY this JSON, nothing else:
   > `{"agent": "gp1", "sum": 4}`

Capture the JSON each subagent returns.

### Step 2 — Sequential: invoke /call A with forked context

After both subagents have returned, invoke:

> `/call` with `context="fork"`, task: "Run /task-a and return its JSON result."

`A` will internally fan out to B, C, D in parallel; C will further nest into E and F.
Capture the JSON `A` returns.

### Step 3 — Join: verify the combined result

Assemble the top-level result:

```json
{
  "agent": "main",
  "S":     { "agent": "S",    "value": "S-result" },
  "gp1":   { "agent": "gp1",  "sum": 4 },
  "A": {
    "agent": "A",
    "B": { "agent": "B", "data": "b-fresh" },
    "C": {
      "agent": "C",
      "E": { "agent": "E", "data": "e-forked" },
      "F": { "agent": "F", "data": "f-result" }
    },
    "D": { "agent": "D", "data": "d-leaf" }
  }
}
```

Then verify and report PASS/FAIL for each check:

- `S.value == "S-result"`
- `gp1.sum == 4`
- `A.B.data == "b-fresh"`                   (B ran with fresh context)
- `A.C.E.data == "e-forked"`                (E ran forked under C)
- `A.C.F.data == "f-result"`                (F ran as a subagent under C, after E)
- `A.D.data == "d-leaf"`                    (D leaf)

Report each as `PASS` or `FAIL <reason>`.
