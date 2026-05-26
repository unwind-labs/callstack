---
name: task-d
description: Leaf D — invoked with forked context. Returns a fixed JSON marker so the orchestrator can verify the forked-context /call path worked.
---

# /task-d — Leaf (forked context)

You were invoked via `/call` with `context="fork"`. Just return the fixed result
below — no other work, no other output.

## Output

Return ONLY this JSON (no prose, no code fences):

```
{"agent": "D", "data": "d-leaf"}
```
