# Parallel Calls Example

This example demonstrates parallel fork/join with nested parallelism.

## Call Tree

```
A (orchestrator — this agent)
├── B  (weather report — leaf)
├── C  (market brief — fork node)
│   ├── E  (exchange rates — fork node)
│   │   ├── G  (JPY rate — leaf)
│   │   └── H  (GBP rate — leaf)
│   └── F  (news headlines — leaf)
└── D  (stock report — leaf)
```

## Workflow

When the user says "run", execute the following:

### Step 1 — Fork: call B, C, D in parallel

Use `/call` with parallel mode to run all three tasks concurrently:

> Run /task-b, /task-c, and /task-d in parallel.

Agent C will itself fork to call E and F in parallel before returning.

### Step 2 — Join: collect and validate results

Once all three return, present the combined result as JSON:

```json
{
  "agent": "A",
  "B": { "agent": "B", "tokyo": {...}, "london": {...} },
  "C": { "agent": "C", "exchange_rates": {...}, "news": {...} },
  "D": { "agent": "D", "AAPL": {...}, "GOOGL": {...} }
}
```

Verify:
- B's result contains weather data for Tokyo (temp_c: 22) and London (temp_c: 14)
- C's result contains exchange rates from E (JPY rate: 149.50) AND news from F (tech headline present)
- D's result contains stock prices for AAPL (price: 195.50) and GOOGL (price: 178.25)

Report PASS or FAIL for each check.
