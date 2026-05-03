# /task-e — Exchange Rates (fork node)

Compile exchange rates by delegating to two sub-agents in parallel.

## Procedure

Use `/call` to run both sub-tasks in parallel:

> Run /task-g and /task-h in parallel.

Wait for both to complete. Combine their results.

## Output

Return a JSON object:
```json
{"agent": "E", "JPY": {from G}, "GBP": {from H}}
```
