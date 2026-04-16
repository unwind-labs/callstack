# /task-c — Market Brief (fork node)

Compile a market brief by delegating to two sub-agents in parallel.

## Procedure

Use `/call` to run both sub-tasks in parallel:

> Run /task-e to get exchange rates and /task-f to get news headlines.

Wait for both to complete. Combine their results.

## Output

Return a JSON object:
```json
{"agent": "C", "exchange_rates": {from E}, "news": {from F}}
```
