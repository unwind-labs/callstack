# RFC: Harvest-on-demand process management

Status: **DEFERRED (2026-05-27)** — the tree-wide fork-budget primitive
and all related code (`fork_budget.py`, `invoke_registry.py`,
`CALLSTACK_MAX_CONCURRENT_FORKS`, the in-flight semaphore) have been
removed. This RFC and its findings doc (`SPIKE-A-FINDING.md`) are
retained as design notes for whenever a global live-process bound is
revisited; the load-bearing assumption (claude `--resume` consumes an
injected `tool_result`) was empirically falsified — see spikes A/A2/A3.

Author: amolk
Date: 2026-05-27

## Problem

`CALLSTACK_MAX_CONCURRENT_FORKS` is intended to bound the number of
concurrent `claude` subprocesses spawned by a callstack tree. Today it
does not actually do this:

- `fork_budget.py` caps **acquire-time concurrency** — i.e. how many
  cold spawns happen in parallel — not how many processes are alive
  simultaneously. Sync-blocked parents hold their slots indefinitely.
- `channel_pool.py` keeps every spawned claude alive in a per-session
  LRU pool, even when nothing will ever resume that session. The pool
  caps at `CALLSTACK_MAX_CONCURRENT_FORKS`, so steady-state alive
  count = up to `2 × cap` (in-flight + dead-stock pool reserves).
- A naïve "release slot on sync-call entry; reacquire on return"
  scheme deadlocks (caller and child both queued waiting for slots
  no one will release).

Observed on `agent-callstack-paper` d=6 b=2 Sonnet callstack run with
cap=16: 32 live claudes (1 root + 16 in-flight children + ~15 pool
reserves), 9.5 GB RSS. Cell did not complete in 45 min wall budget.
Larger cells (d=7 b=2 = 255 nodes, d=8 b=2 = 511 nodes) cannot fit at
all on 24 GB hardware regardless of cap.

The paper has zero callstack data above 121 nodes (d=4 b=3). All
larger reports are flat-paradigm only (single-process).

## Goals

1. **Bound live `claude` count at ≈ `CALLSTACK_MAX_CONCURRENT_FORKS`**
   for sync-call-dominated workloads (the common case, including the
   paper).
2. **Never deadlock** — new spawns always make progress.
3. **No new public API surface** — same `mcp__plugin_callstack_call__*`
   tools, same env knobs.
4. **Resume cost is opt-in** — sub-cap workloads pay nothing; only
   memory-pressured trees pay the `--resume` cost.
5. **Mental model fits one paragraph** so operators can reason about
   it without reading the plugin source.

## Non-goals

- Bounding pure-async workloads (`run_in_background=True` chains where
  every parent stays actively running). These genuinely need every
  process alive; soft-cap-with-overage is correct behavior. Log a
  warning when the soft cap is exceeded; don't try to harvest a
  process that is doing real work.
- Cross-session pool reuse (the "session-rebind" idea). Orthogonal;
  this RFC does not preclude it but doesn't require it.
- Cross-machine scheduling. Out of scope, same as `fork_budget`.

## Mental model

A `claude` subprocess in a callstack tree is in one of two states:

- **Active** — currently consuming CPU/IPC. The process is doing model
  inference, tool calls, or producing output. Cannot be harvested.
- **Harvestable** — sync-blocked inside a `/call` waiting for child
  results. The process exists but is idle (no model turn in flight).
  Its on-disk transcript fully captures its state, so it can be
  killed and rebuilt later via `claude --resume <session_id>`.

On every new `/call` spawn, if live process count would exceed the
cap, the oldest harvestable process is killed first. New spawns
always proceed. When a child returns to a parent whose process was
harvested, the plugin transparently re-spawns the parent via
`--resume` and injects the child's result into the resumed input
stream.

Steady-state: `live_processes ≈ cap`, regardless of tree depth or
node count.

## Design

### State transitions

```
                          spawn
                       ┌─────────┐
                       │         ▼
                  ╔═══════════════════╗
                  ║      Active       ║
                  ╚═══════════════════╝
                       ▲             │
                       │             │ enter sync /call
              receive  │             │ (await child results)
            all child  │             │
              results  │             ▼
                  ╔═══════════════════╗
                  ║   Harvestable     ║
                  ╚═══════════════════╝
                       │             │
                       │             │ kill (memory pressure)
                       │             ▼
                  ╔═══════════════════╗
                  ║     Harvested     ║───── --resume + inject ──┐
                  ╚═══════════════════╝                          │
                                                                 ▼
                                                          back to Active
                                                          (transcript replays)
```

### Process lifecycle

1. **Spawn (entering Active).** Plugin attempts to register a new
   process slot. If `live_count >= cap`, find the
   least-recently-marked-harvestable process across the tree and
   kill it (it becomes Harvested). Then spawn the new claude.
   Process is now Active.

2. **Issue async /call** (`run_in_background=True`). Caller stays
   Active. Child becomes Active (via step 1). Both consume slots.

3. **Issue sync /call** (`run_in_background=False` — the common
   case). Caller transitions to **Harvestable** immediately, then
   children spawn (step 1). When all child results are collected,
   caller transitions back to **Active** (it may need to be
   re-resumed from Harvested first; see step 5).

4. **Child returns.** Result is **always** written to the persistent
   invoke-registry (keyed by `invoke_id`), **never** delivered
   directly to the parent's MCP call. The parent's await mechanism
   reads from the registry. This makes harvest race-free.

5. **Re-acquire after sync wait.** When all results for a Harvestable
   parent's pending children are in the registry:
   - If the parent process is still alive (was not harvested): wake
     it from its MCP await, deliver bundled results, transition to
     Active.
   - If the parent was Harvested: enter spawn flow (step 1; may
     evict another harvestable), `claude --resume <parent_session>`,
     inject the bundled `tool_result` for the original /call's
     `tool_use_id` via stream-json. Parent claude continues from
     the tool_result as if it had never been killed.

6. **Process exit (entering terminal).** Process completes its work,
   returns its value, exits. Slot is released. If the process had
   been Harvested and never resumed (e.g. its caller died), nothing
   to do — the persistent transcript and invoke registry can be
   garbage-collected by a later sweep.

### Eviction policy

When eviction is needed:

1. Enumerate all processes currently in **Harvestable** state.
2. Pick the one with the **oldest harvestable-since timestamp** (LRU
   on harvestability transitions).
3. Kill it (SIGTERM, then SIGKILL after grace period).
4. Mark it Harvested in the plugin's process registry.

If no Harvestable processes exist, **spawn anyway** and log a
warning (`callstack: live_count=N exceeds cap=M, no harvestable
processes available`). This handles the pure-async case correctly.

### Invoke registry (the result-routing primitive)

Single source of truth for in-flight call results. One entry per
`/call` invocation, keyed by `invoke_id`. Persistent in the
per-root-session directory (e.g.
`~/.callstack/sessions/<root_id>/invokes/<invoke_id>.json`).

Schema:

```json
{
  "invoke_id": "...",
  "parent_session_id": "...",
  "parent_tool_use_id": "...",   // the /call MCP tool_use id in parent's transcript
  "child_tasks": [
    {"task_index": 0, "child_session_id": "...", "result": null, "status": "pending"},
    {"task_index": 1, "child_session_id": "...", "result": null, "status": "pending"}
  ],
  "created_utc": "...",
  "completed_utc": null,
  "delivered_utc": null
}
```

Lifecycle:
- Created when parent issues `/call`.
- Each child task's `result` filled in as that child returns.
- When all tasks `status: "completed"`, registry is "ready to
  deliver." Parent re-acquires (step 5).
- `delivered_utc` stamped after parent resumes and reads the
  bundled result.
- File preserved on disk until later GC sweep (forensic audit).

Because the registry is the single source of truth, the parent's
process can be killed and reborn at any time without losing data.

## Implementation sketch

### Files to change

| file | change |
|---|---|
| `fork_budget.py` | rename/repurpose: instead of an "acquire on spawn / release on exit" semaphore, track live processes by PID with state metadata (Active / Harvestable / Harvested). Add `find_oldest_harvestable()` and `try_evict(pid)`. |
| `channel.py` | call `fork_budget.register_active(pid)` on spawn; transition to `mark_harvestable(session_id)` when entering sync `/call` wait in `mcp_server.py`. |
| `channel_pool.py` | **delete**, or reduce to a thin shim that closes processes immediately after each turn. The harvest mechanism replaces pooling. |
| `mcp_server.py` | sync-call path: write to invoke-registry, mark parent harvestable, wait for registry completion. Re-spawn / inject path for harvested parents. |
| `env.py` | optional: `CALLSTACK_HARVEST_DISABLE` for opt-out (default off). |

### Invoke-registry write path

```python
# Inside mcp_server.py, sync /call handler (pseudocode)
async def handle_sync_call(tool_use_id: str, parent_session_id: str, tasks: list):
    invoke_id = uuid4().hex
    registry.create(invoke_id, parent_session_id, tool_use_id, tasks)
    fork_budget.mark_harvestable(parent_session_id)
    try:
        for task_index, task in enumerate(tasks):
            child_session_id = await spawn_child(task)
            registry.attach_child(invoke_id, task_index, child_session_id)
        results = await registry.wait_until_complete(invoke_id)
        # At this point, parent may or may not still be alive.
        # Either way, results are durable in the registry.
        return bundle_results(results)
    finally:
        fork_budget.mark_active(parent_session_id)  # if still alive
```

### Re-spawn + inject path

```python
# Triggered when a Harvested parent's registry entry becomes complete.
def revive_harvested(parent_session_id: str, invoke_id: str):
    fork_budget.register_active_evict_if_needed()
    proc = spawn_claude(
        args=["--resume", parent_session_id, "--output-format", "stream-json",
              "--input-format", "stream-json", ...],
        ...
    )
    bundled = registry.get_bundle(invoke_id)
    inject_tool_result(
        proc.stdin,
        tool_use_id=registry.lookup_tool_use_id(invoke_id),
        content=bundled,
    )
    # proc takes over from here; subsequent /call tool calls from
    # the resumed claude flow through the normal MCP path.
```

The transcript on disk already contains the /call `tool_use`
message; `--resume` replays it. The injected `tool_result` is the
NEXT message after the /call, so the resumed claude sees the
expected message sequence.

## Correctness concerns

### Race: child returns mid-harvest

Resolved by the invariant: **child results go to the registry
first, never directly to parent's MCP call.** So:

- Plugin decides to evict parent A. Plugin acquires
  `fork_budget` lock, marks A `Harvested`, kills A's process.
- Concurrently or before, A's child returns: result is written to
  `registry[invoke_id].child_tasks[i].result`. Atomic file write.
- Whichever happens first, no data is lost. When the registry
  detects all child slots are filled, it triggers revive.

### Re-acquire when registry completes and cap is full

If all cap slots are held by Active processes and a Harvested parent
becomes ready to revive, the plugin must evict yet another
harvestable. Cascade is possible but bounded — each revive frees one
slot when its work eventually completes.

In the pathological case where every slot is held by an Active
process and a Harvested parent wants to come back, but no
harvestables exist to evict, the parent waits in a "ready to
revive" queue. Forward progress is guaranteed because some Active
process will eventually exit or enter sync /call (becoming
harvestable). No deadlock.

### `claude --resume` integrity

Depends on the CLI preserving session transcripts across kills.
Verified empirically: `~/.claude/projects/<encoded-cwd>/sessions/`
files survive arbitrary process termination. The injected
`tool_result` must address the correct `tool_use_id` — recoverable
by scanning the transcript JSONL for the last unanswered
`mcp__plugin_callstack_call__call` tool_use.

### Async-call children outliving harvested parents

An async-call result is still valid even if its caller was
harvested and the caller's *grandparent* is now also harvested. The
chain unwinds through the registry: each level's results sit in
their own invoke_id entry, revives happen in reverse-call-stack
order as results bubble up.

If a caller exits without awaiting an async child, the child's
result is orphaned in the registry. GC'd by a later sweep.

## Performance characteristics

Sub-cap workloads (typical): zero overhead — no eviction, no
revives, no registry writes for the spawn-path (only for sync /call
boundaries which would have been MCP overhead anyway).

Memory-pressured workloads: each evict-then-revive cycle costs one
extra `claude --resume` spawn (~1-3 s + transcript load time, which
scales with frame depth). For d=6 b=2 (127 nodes) with cap=16:
worst case ~63 internal frames revive once each, ~3-5 min added
wall time. For d=8 b=2 (511 nodes), ~255 revives, ~12-15 min.

Live RAM during a memory-pressured run: ≈ `cap × per_proc_RSS`.
For cap=16 × 290 MB ≈ 5 GB. Fits in 24 GB hardware with all
larger paper cells.

## Alternatives considered

### Slot-handoff with priority queue (release on sync-call entry, reacquire on return)

Equivalent caps. Adds a priority queue for "returners go before fresh
callers" to avoid livelock. More code (queue, priorities, atomic
release-reacquire). Harvest-on-demand subsumes this design by
removing the queue entirely.

### Just disable the cap (let it spawn unbounded)

Trivial. Works at d ≤ 6 on 24 GB. Fails at d ≥ 7 (RAM exceeds
physical + swap). Adequate for paper cells we can run; doesn't
unlock the bigger ones.

### Session-agnostic warm pool (cross-session process reuse)

Would also bound live count, but requires upstream CLI change to
support session-rebind via stream-json control message. Two-quarter
project. Orthogonal — harvest-on-demand works without it; both
together would eliminate even the per-revive resume cost.

### `CALLSTACK_POOL_SIZE=0` (disable pooling, keep current spawn semantics)

Halves the dead-stock overhead (saves the ~16 pool reserves) but
doesn't address the in-flight sync-blocked parents holding processes.
For d=6 b=2 cap=16: drops alive count from 32 → 16+sync_blocked.
With branching=2 fully unrolled, sync-blocked = 63 internal nodes
→ 79 alive. Doesn't fit. Insufficient.

## Acceptance criteria

### Functional

1. New unit test `tests/test_harvest.py`:
   - A synthetic tree of depth 4, branching 2 (15 nodes) with
     cap=4 completes successfully. Result matches the same tree
     run with cap=64 (no eviction needed). Verifies harvest +
     revive is value-preserving.

2. New unit test for race resilience:
   - Mock a child result arriving exactly when its parent is being
     harvested. Result is correctly delivered to the resumed parent.

3. Updated test `tests/test_fork_budget.py`:
   - Existing tests pass with the new "register/mark_harvestable/
     try_evict" API in place of "acquire_fork_slot/release_fork_slot".

4. New unit test `tests/test_invoke_registry.py`:
   - Concurrent writes from multiple child sessions land atomically.
   - `wait_until_complete` returns when all task slots are filled.
   - Registry persists across plugin process restarts (resume
     mid-flight if the orchestrator crashes).

### Operational

5. `agent-callstack-paper` d=6 b=2 callstack Sonnet run with cap=16
   completes in < 45 min wall on a 24 GB machine. Watcher shows
   live claude count ≈ cap throughout. Mem stays under 6 GB.

6. `agent-callstack-paper` d=8 b=2 callstack Sonnet run with cap=16
   completes in < 90 min wall on a 24 GB machine. Live count stays
   ≈ cap; revives logged (~250 expected).

7. d=4 b=2 callstack (already passing) remains within 2× of its
   current wall time — verifies sub-cap workloads pay near-zero
   overhead.

### Observability

8. Plugin emits structured log events for `harvest` and `revive`
   with `invoke_id`, `session_id`, `harvestable_since` (for the
   eviction LRU verification).

9. New metric in the per-root-session directory:
   `~/.callstack/sessions/<root>/metrics.json` records
   `{spawns: N, harvests: M, revives: K, peak_live: P, peak_RSS_mb: R}`.

## Out of scope (this RFC)

- Pre-emptive harvest based on RAM pressure rather than just
  process-count cap. Could be a follow-up if cap is a poor proxy
  for the actual RAM constraint.
- Per-process RSS-aware eviction (kill the largest harvestable
  instead of the oldest). Defer until LRU is shown to waste resume
  cost in practice.
- Cross-host harvest (would need a different transport).
- GC sweep for orphaned invoke registry entries. Add when forensic
  directories get inconveniently large.
