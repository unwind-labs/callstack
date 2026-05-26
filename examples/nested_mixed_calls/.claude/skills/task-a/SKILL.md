---
name: task-a
description: Orchestrator A — fans out to B (fresh), C (forked), D (forked) in parallel and joins their results.
---

# /task-a — Orchestrator (fork node)

Fan out to three children **in parallel** using `/call`. Issue all three in a single
message so they run concurrently:

- `/call /task-b` with `context="fresh"`
- `/call /task-c` with `context="fork"`
- `/call /task-d` with `context="fork"`

Wait for all three to return, then combine their JSON outputs.

## Output

Return ONLY this JSON (no prose, no code fences):

```
{"agent": "A", "B": {from B}, "C": {from C}, "D": {from D}}
```
