---
name: task-e
description: Leaf E — invoked from C with forked context. Returns a fixed JSON marker so the orchestrator can verify the deep forked /call path worked.
---

# /task-e — Leaf (forked, nested under C)

You were invoked via `/call` with `context="fork"` from agent C. Just return the
fixed result below.

## Output

Return ONLY this JSON (no prose, no code fences):

```
{"agent": "E", "data": "e-forked"}
```
