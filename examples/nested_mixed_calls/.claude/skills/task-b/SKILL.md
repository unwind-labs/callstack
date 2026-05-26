---
name: task-b
description: Leaf B — invoked with fresh context. Returns a fixed JSON marker so the orchestrator can verify the fresh-context /call path worked.
---

# /task-b — Leaf (fresh context)

You were invoked via `/call` with `context="fresh"`, so you do **not** have the
caller's conversation. Just return the fixed result below.

## Output

Return ONLY this JSON (no prose, no code fences):

```
{"agent": "B", "data": "b-fresh"}
```
