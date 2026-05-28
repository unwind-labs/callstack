# PLAN: Harvest-on-demand process management

Status: **DEFERRED (2026-05-27)** — see header in
`RFC-harvest-on-demand.md`. Phase 1 (invoke_registry) was implemented
but then removed when the spikes invalidated Phase 4. This plan is
retained as a starting point if the design is revisited.

Author: amolk (with Claude)
Date: 2026-05-27
RFC: [dev/RFC-harvest-on-demand.md](RFC-harvest-on-demand.md)

This is the implementation plan for the RFC. It locks in design choices
the RFC left open, phases the work, and lists the empirical spikes
required before any irreversible refactor.

## 1. Design choices locked in by this plan

### 1.1 Orchestrator: existing child-return path, no new daemon

The RFC says "the plugin decides to evict" / "triggered when registry
becomes complete" without naming the process. Today's `driver.py`
already routes child→parent state via `_propagate_up` (emitting
`ChildDone` / `ChildFailed` events). **We extend that path** rather
than add a supervisor.

Concretely:

- Each child's MCP server (the one running inside the child claude
  whose result is bubbling up) is the trigger.
- Before the child claude exits, its MCP server writes its `Result`
  to the parent's invoke-registry entry. The write is atomic
  (temp + rename inside a per-invoke flock).
- The write returns `pending_remaining` after the update. If
  `pending_remaining == 0` AND the parent's process is in state
  `Harvested`, the child's MCP server double-forks a detached
  respawner (`claude --resume <parent> --input-format stream-json`),
  writes the bundled `tool_result` JSON to its stdin, closes stdin,
  and exits. The respawner becomes the new parent claude.

No long-lived daemon. The orchestrator is implicit: every MCP server
carries the revive code, and exactly one (the writer of the last
result) fires it per parent.

### 1.2 Invoke registry as the persistence layer

Same shape as the RFC §"Invoke registry". One JSON file per `invoke_id`
under `~/.callstack/sessions/<root>/invokes/`. Atomic writes via temp+
rename; readers use a per-file flock to serialize complete-detection.

The registry is the **only** new persistent surface; everything else
re-uses existing on-disk state (claude transcripts, frames.jsonl).

### 1.3 fork_budget becomes a process registry, semaphore semantics preserved

`fork_budget.py` currently writes `<pid>.token` empty touch-files. We
extend the per-slot sidecar with state metadata:

```
~/.callstack/sessions/<root>/
  active/
    <pid>.<token-id>.json    {"state": "active"|"harvestable"|"harvested",
                              "session_id": "...",
                              "harvestable_since_utc": "...",
                              "invoke_id_waiting_on": "..." }
```

(We change the on-disk filename suffix `.token` → `.json` so the new
format is unambiguous and a partial-rollout reader can't confuse the
two; the migration is one-shot — the old tokens are owned by processes
that died before the upgrade and would be reaped on the next acquire
regardless.)

`acquire_fork_slot` keeps its current signature. New entry points:
- `mark_harvestable(session_id, invoke_id)` — flips state, stamps
  `harvestable_since_utc`.
- `mark_active(session_id)` — flips back; clears the timestamp.
- `find_oldest_harvestable(exclude_session_ids=…) -> Optional[Entry]`
  — LRU candidate for eviction.
- `evict(entry) -> bool` — SIGTERM, then SIGKILL after grace, then
  flip state to `harvested`. Returns False if process was already
  gone.

`acquire_fork_slot` is the policy point: if `live_count >= cap`, it
calls `find_oldest_harvestable()` and `evict()` until a slot frees,
or spawns over-cap and logs a warning if none exists.

### 1.4 channel_pool: keep, but default size 0

The RFC says "delete or reduce to a shim." Deleting orphans the
single-process reuse optimization (legitimate within one MCP server's
lifetime — the 3-6 resume-mode turns per node). We instead make the
default pool size 0, controlled by a new env knob
`CALLSTACK_POOL_SIZE` (default `0`). Existing tests that exercise
pool-hit paths can set it explicitly. This is a separate phase from
the harvest mechanic and lands first as a quick win — it already
unblocks d=6 b=2 cap=16 in isolation per the RFC's "Alternatives"
analysis (which said it's insufficient alone, but it's a real ~50%
RSS reduction).

### 1.5 Process group semantics — child claudes must survive parent kill

Today, child claudes spawned by a parent's MCP server inherit the
parent's process group. When `evict()` SIGKILLs the parent claude,
the children inherit SIGHUP and die. This makes harvest pointless.

Fix: in `ClaudeChannel._spawn`, pass `start_new_session=True` to
`subprocess.Popen` so each child claude becomes its own session
leader. Verified compatible with the existing `permission_mode`
stdio handshake (the MCP server's stdio is to its OWN claude, not
to the children — children are themselves Popen'd by `_spawn`).

## 2. Empirical spikes (required before phase 4)

These must succeed in a throwaway script before we commit to the
respawn+inject path. If any fails, the RFC's plan B is necessary.

### Spike A — `claude --resume` + stream-json tool_result injection

Reproduce:
1. Start a claude session that calls a known MCP tool, sync.
2. Capture its session id while it's blocked waiting for tool_result.
3. SIGKILL the claude process.
4. Run `claude --resume <id> --input-format stream-json --output-format stream-json`.
5. Send a single NDJSON line: `{"type":"user","message":{"role":"tool","content":[{"type":"tool_result","tool_use_id":"<id>","content":"..."}]}}`
6. Verify the resumed claude continues from the tool_result.

This is the highest-risk piece. If claude's `--resume` rejects a
`tool_result` as the first message-after-resume, we need to fall
back to either (a) injecting via a wrapping assistant message
(probably model-visible), (b) waiting for an upstream CLI feature,
or (c) abandoning revive in favor of "kill parent, restart with the
result text prepended to a fresh prompt" — semantically lossy.

Owner: amolk (must run before Phase 4 starts).

### Spike B — tool_use_id recoverability

In the parent's transcript JSONL at
`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`, scan for the
last `mcp__plugin_callstack_call__call` tool_use whose
`tool_use_id` has no matching `tool_result`. Confirm the file format
and that we can identify it deterministically across CLI versions.

Owner: amolk.

### Spike C — process-group isolation

After setting `start_new_session=True`, kill a parent claude and
verify its children continue running and emit their stream-json
output for at least 30 s.

Owner: amolk.

## 3. Phasing

Phases land independently and each is value-positive on its own.

### Phase 1 — invoke_registry module (additive)

**Goal:** persistent result-routing primitive, no behavioral change
to live runs (nothing calls it yet).

Files:
- NEW `agent_callstack/invoke_registry.py` — schema, `create`,
  `attach_child`, `complete_task`, `wait_until_complete`,
  `get_bundle`, `lookup_tool_use_id`. Per-invoke flock for
  complete-detection.
- NEW `tests/test_invoke_registry.py` — atomic concurrent writes,
  wait/complete, persistence across simulated process restart.

Acceptance: tests pass; module is importable but unused.

### Phase 2 — channel_pool default size 0 (quick win)

**Goal:** remove dead-stock pool reserves from the steady-state live
count. RFC §Alternatives shows this halves overhead at the cost of
some warm-pool reuse latency in long single-process runs.

Files:
- `channel_pool.py` — new `CALLSTACK_POOL_SIZE` env, default 0.
- `env.py` — accessor.
- `tests/test_channel_pool*.py` — assert default 0, opt-in by env.

Acceptance: existing test suite passes (some tests may need explicit
opt-in); pool size 0 is the default in live runs.

### Phase 3 — fork_budget → process_registry refactor

**Goal:** track per-slot state and the harvestability LRU. No
eviction wired in yet — `mark_harvestable` is a no-op observer.

Files:
- `fork_budget.py` — rename internals to "process registry"; new
  JSON sidecar; new entry points (`mark_harvestable`,
  `mark_active`, `find_oldest_harvestable`, `evict`).
  `acquire_fork_slot` / `release_fork_slot` keep their signatures.
- `channel.py` — keep call sites; no functional change yet.
- `tests/test_fork_budget.py` — update for new sidecar format;
  add tests for `mark_harvestable` LRU ordering and `evict()`
  signal sequence (mock subprocess).

Acceptance: existing fork_budget tests pass against new internals;
new tests cover state transitions.

### Phase 4 — wire sync-call boundary to registry + harvest

**Goal:** sync /call writes registry, marks parent harvestable,
waits on registry; eviction fires when over cap. **This is the
phase that changes behavior under load.**

Files:
- `mcp_server.py` — sync-call path writes invoke-registry, marks
  parent harvestable while waiting on `wait_until_complete`,
  marks active on return.
- `fork_budget.py` — `acquire_fork_slot` now evicts oldest
  harvestable when over cap (instead of spinning).
- `channel.py` — `_spawn` uses `start_new_session=True` (Spike C).
- `agent_callstack/__init__.py` / `driver.py` — child's terminal
  state writes its `Result` to the invoke-registry before the
  outer MCP `call` returns. If `pending_remaining == 0` and parent
  state is `harvested`, double-fork a respawner (Spike A).
- `tests/test_harvest.py` — depth-4 b=2 synthetic tree with cap=4
  completes; result matches cap=64 oracle. Race test: child
  result arrives during eviction.

Acceptance: synthetic test passes; cap is bounded in unit tests
using `ScriptedChannel`-derived process surrogates.

### Phase 5 — operational verification (out-of-session)

User-driven, not in this implementation session:
- d=4 b=2 wall time within 2× of baseline (Acceptance §7).
- d=6 b=2 cap=16 completes in < 45 min on 24 GB (§5).
- d=8 b=2 cap=16 completes in < 90 min on 24 GB (§6).
- `metrics.json` populated with `peak_live`, `harvests`,
  `revives`, etc. (§9).

## 4. Resolved questions (amolk, 2026-05-27)

1. **Spike A** — amolk has not validated `tool_result` injection;
   Claude (this session) is to run the spike. **This gates Phase 4.**

2. **Phase 4 commit barrier** — none. No external consumers of
   callstack yet, so the sync-call path semantics change unconditionally
   when Phase 4 lands. No `CALLSTACK_HARVEST` opt-in gate.

3. **Cascade depth** — accepted. Each revive does real work before
   returning, so a chain of respawns is not wasteful in the way a
   pure event-bubble would be. No dedicated cascade test required.

4. **channel_pool** — delete after harvest lands. Phase 2 becomes
   "remove channel_pool" rather than "default size 0". Whatever still
   needs single-process-per-session reuse keeps a thin per-MCP cache
   inside `channel.py`; if nothing needs it, the module goes.

## 5. Test strategy summary

| Phase | Test file | Coverage |
|---|---|---|
| 1 | tests/test_invoke_registry.py | atomic CW, completion detection, persistence |
| 2 | tests/test_channel_pool*.py | default size 0, opt-in by env |
| 3 | tests/test_fork_budget.py | state transitions, LRU, evict() signal sequence |
| 4 | tests/test_harvest.py | synthetic tree value-preservation, race resilience |
| 5 | manual / paper runs | d=6, d=8, baseline regression |
