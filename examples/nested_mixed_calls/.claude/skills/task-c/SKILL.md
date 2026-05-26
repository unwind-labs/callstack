---
name: task-c
description: Nested fork C — first /call /task-e (forked), then launch a general-purpose subagent F after E returns, and join the two results.
---

# /task-c — Nested fork (sequential E → F)

Do these two steps **in order** (not in parallel — F depends on E having returned):

### Step 1 — `/call /task-e` with `context="fork"`

Capture the JSON E returns.

### Step 2 — After E returns, launch a general-purpose subagent F

Use the **Agent tool** with `subagent_type: "general-purpose"` and the prompt:

> You are agent **F**, launched after agent E finished. Return ONLY this JSON,
> nothing else: `{"agent": "F", "data": "f-result"}`

Capture the JSON F returns.

## Output

Return ONLY this JSON (no prose, no code fences):

```
{"agent": "C", "E": {from E}, "F": {from F}}
```
