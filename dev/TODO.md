# Tasks

- [ ] When a new node is created, we auto zoom to fit. Instead lets focus it as if we used keyboard arrow to select the node
- [ ] If a session is rendered as multiple window nodes. For example, T1-T3, T4-T8, T9-. We want to render the nodes such that the are "open" where activity continues in another node. We show openness by showing a torn piece of paper type visual - the simplest would be removing the top/bottom border and making corresponding border radius 0. So T1-T3 node will have normal top border, but "open" bottom because activity continues into T4-T8. The T4-T8 node will have both top and bottom "open". T9- node will only have top "open". Bottom for T9- will be closed if the node status is completed, otherwise it will be "open".

---

# Architecture review — 2026-05-13

From [the full review plan](~/.claude/plans/do-a-full-architecture-partitioned-popcorn.md).
Each item cites file:line and includes a concrete fix. Execute via `/call`,
one at a time, top-down.

## P0 — fix before next release

- [x] **SEC-001** Default `ClaudeChannel.permission_handler` to deny-by-default with a read-only allowlist; require explicit opt-in for `allow_all`. `plugins/callstack/agent_callstack/channel.py:67,103` — **won't fix**: forked children inherit parent's authority by design. Keep `allow_all` as the default. Document this threat model in README so users understand fork-via-MCP is equivalent to giving the caller agent shell access.
- [x] **SEC-012** Harden `_resolve_cwd`: `Path.resolve(strict=True)`, parent-project allowlist, reject `/etc`, `~/.ssh`, `~/.aws`, etc. `plugins/callstack/mcp_server.py:103-119` — commit `4a16788`.
- [x] **PERF-D** Reuse the `claude` subprocess across consecutive `resume`-mode turns of the same session. `plugins/callstack/agent_callstack/channel.py:106-156` — commit `bfda439`. Note: multiplexed-pool ruled out — stream-json strips slash commands, so a single process cannot switch session via `/resume`. Pool is process-PER-session, LRU-evicted.
- [x] **PERF-A** Debounce `_LiveReporter._notify` (~250 ms), maintain an in-memory merged tree, skip rewrite when content hash unchanged. `plugins/callstack/agent_callstack/__init__.py:508-545` — commit `05fcc29`.
- [x] **ARCH-1** Split `agent_callstack/__init__.py` (884 LOC) into `reporter.py`, `frames.py`, `invocation_ctx.py`, `results.py`. Keep `__init__.py` as the public facade. — commit `8a6d4d1`. After-state: __init__.py 378, reporter.py 279, frames.py 315, invocation_ctx.py 89, results.py 105.
- [x] **ARCH-2** Unify the three near-identical tree walkers (`_graft_node`, `_walk_tree`, `_graft_raw`, `_chain_to_session`) behind one `walk(nodes, visit, *, with_chain)` generator. — commit `7d16b91` (partial). Extracted `_grafted_children` helper shared by `_graft_node` and `_graft_raw`. Full generator-based unification was tried and reverted: it net-added LOC without improving clarity. `_walk_tree` left alone (different node shape).
- [x] **ARCH-3** Build a one-shot `parent_index` + `depth_index` at `_propagate_up` entry; replace O(N) recursive walks in `_depth_of`, `_parent_file_for`, `_find_parent`. `plugins/callstack/agent_callstack/driver.py:292,307,325,352` — commit `571f4c6`. `_TreeIndex` built per propagate. O(D²) → O(N+D).

## P1 — important hardening / cleanup

### Security
- [x] **SEC-002** Constrain `CALLSTACK_PARENT_SESSION` to paths under `PROJECTS_DIR` via `resolve(strict=True).is_relative_to(...)`. `plugins/callstack/agent_callstack/session.py:21,100-116` — commit `e676b8f`.
- [x] **SEC-004** Move subprocess logs out of `/tmp` to `~/.claude/callstack/log/<invoke_id>/` (or use `tempfile.NamedTemporaryFile` with mode 0600). `plugins/callstack/agent_callstack/channel.py:126-128` — commit `e676b8f`.
- [x] **SEC-007** Make `TreeStore.load` race-safe: `os.replace(path, claim)` before reading; `contextlib.suppress(FileNotFoundError)` for unlink. `plugins/callstack/agent_callstack/trace.py:84-91` — commit `e676b8f`.
- [x] **SEC-008** Replace fixed `.tmp` suffix in `_atomic_yaml_write` with `tempfile.NamedTemporaryFile`; add `flush + fsync` before `os.replace`. `plugins/callstack/agent_callstack/__init__.py:603-609` — commit `e676b8f`.
- [x] **SEC-011** Narrow `except Exception: pass` blocks (channel, driver, frame loader); log via module logger; fail-closed for permission handler. `plugins/callstack/agent_callstack/channel.py:318`, `driver.py:278`, `__init__.py:628` — commit `e676b8f`.

### Performance
- [x] **PERF-B** Cache `_load_frames` by `(path, st_mtime_ns, st_size)`; consider JSON for frame format. `plugins/callstack/agent_callstack/__init__.py:614-637` — commit `05fcc29` (stat cache; JSON format deferred).
- [x] **PERF-C** Maintain mutable indexed merged tree; patch on transition instead of rebuilding via `_build_merged_report` per tick. `plugins/callstack/agent_callstack/__init__.py:640-672` — **skipped**: PERF-A's debounce + content-hash skip already captures the win at acceptable rebuild frequency.
- [x] **PERF-E** Replace text-mode `count_lines` with binary chunked newline count, or drop the metric. `plugins/callstack/agent_callstack/session.py:175-181` — commit `9bda93c`.
- [x] **PERF-F** Consolidate the two `_most_recent` implementations; switch to `os.scandir`; memoize per `SessionLocator`. `plugins/callstack/agent_callstack/session.py:131-152`, `__init__.py:824` — commit `9bda93c`.
- [x] **PERF-G** Maintain a lazy `~/.claude/projects/.session_index.json` for `SessionLocator.resolve` instead of scanning every project dir. `plugins/callstack/agent_callstack/session.py:82-96` — commit `9bda93c`.
- [x] **PERF-H** Split fork semaphore into a small `concurrent_spawns` cap and larger `concurrent_in_flight` cap. `plugins/callstack/agent_callstack/channel.py:139-200` — commit `9bda93c`.

### DRY / simplification
- [x] **ARCH-4** Add `Result.to_envelope()` / `CallYielded.to_envelope()` / `CallFailed.to_envelope()`; MCP calls `.to_envelope()` instead of reaching into internal fields. `plugins/callstack/mcp_server.py:35-60`, `__init__.py:351` — commit `d2887d5`.
- [x] **ARCH-5** Replace hand-rolled `Node` / `State` codecs with one `dataclass_codec` helper. `plugins/callstack/agent_callstack/driver.py:90,104,558,562` — **skipped**: discriminated-union `State` + `Node` only; a generic codec would need per-variant serializers or a runtime type-tag, no LOC win.
- [x] **ARCH-6** Split `_InvocationContext` into `RootInvocationContext` + `NestedInvocationContext` behind a small Protocol. `plugins/callstack/agent_callstack/__init__.py:413-470` — **skipped**: class is 90 LOC with 2 conditional lines; the split grows LOC for marginal gain.
- [x] **ARCH-7** Consolidate `_STATUS_FROM_STATE` and `_status_label` into a single `state.status(s: State)` in `state.py`. `plugins/callstack/agent_callstack/__init__.py:728`, `driver.py:568` — commit `d2887d5`.
- [x] **ARCH-8** Move `ScriptedChannel` from `channel.py` to `agent_callstack/testing.py`. `plugins/callstack/agent_callstack/channel.py:1-424` — commit `d2887d5`.
- [x] **ARCH-9** Pre-resolve `SessionRef` once at Caller entry; Driver should never call `locator.resolve`. Cross-cuts `__init__.py` + `driver.py` — commit `d2887d5`.

## P2 — low severity / nice-to-have

- [x] **SEC-003** Constrain `SessionLocator.resolve` to the cwd-matching project dir; require explicit cwd for cross-project. `plugins/callstack/agent_callstack/session.py:82` — commit `7755206`.
- [x] **SEC-005** Cap NDJSON line size (4 MiB) and rotate stderr log. `plugins/callstack/agent_callstack/channel.py:290-304` — commit `7755206`.
- [x] **SEC-006** Cap `_load_frames` count + size; validate `frame_key`; log parse failures. `plugins/callstack/agent_callstack/__init__.py:614-637` — commit `7755206`.
- [x] **SEC-009** Switch `flock` → `fcntl.lockf`; never unlink the lock file; add timeout + retry. `plugins/callstack/agent_callstack/__init__.py:592-600` — commit `7755206`.
- [x] **SEC-010** Acquire `_interprocess_lock` around the `frames_dir` scan too; log parse misses. `plugins/callstack/agent_callstack/__init__.py:534-545` — commit `7755206`.
- [x] **SEC-013** Validate `permission_mode` against known set; validate `source_session_id` is a UUID before `--resume`. `plugins/callstack/agent_callstack/channel.py:230-246` — commit `7755206`.
- [x] **PERF-I** Open `_drain_stderr` log with `buffering=1`; drop explicit flush except on error. `plugins/callstack/agent_callstack/channel.py:167-171` — commit `7755206`.
- [x] **PERF-J** Reuse a module-level `ThreadPoolExecutor` sized to the fork cap, instead of per-`call_many`. `plugins/callstack/agent_callstack/driver.py:224` — commit `7755206`.
- [x] **PERF-K** Use `str.translate` table for `_one_line`; iterative walk in `_walk_tree`. `plugins/callstack/agent_callstack/__init__.py:845,747` — commit `05fcc29` (iterative walk done; `_one_line` translate-table deferred, low impact).
- [x] **ARCH-10** Consolidate MCP helpers into a single `McpInvocationPlan` dataclass resolved once per request. `plugins/callstack/mcp_server.py:71-122` — **skipped**: each helper is small and called from 1–2 sites; the dataclass adds ceremony without saving LOC.
- [x] **ARCH-11** Pick one of `extra_env` / `env_extra` in `ClaudeChannel`; document precedence. `plugins/callstack/agent_callstack/channel.py:93-130,230` — commit `7755206`.
- [x] **ARCH-12** Replace `call` / `call_many` / `resume` wrapper boilerplate with one private `_dispatch` helper. `plugins/callstack/agent_callstack/__init__.py:306-346` — commit `7755206`.
- [x] **ARCH-13** Delete or move legacy `_write_invocation_report` to `tests/_helpers.py` after `git grep` confirms callers. `plugins/callstack/agent_callstack/__init__.py:852-884` — commit `7755206`.
- [x] **ARCH-14** Rename internal `node.result` → `node.payload`; keep `Result.value` as public; map at the boundary. `plugins/callstack/agent_callstack/__init__.py:57,351` — **skipped**: rename churns every read site for marginal clarity gain.

---

# Architecture review — 2026-05-15

From [the full review plan](~/.claude/plans/do-deep-architecural-review-polished-peach.md).
Four parallel review forks (security / perf / DRY-simplicity / correctness)
read the runtime modules read-only and reported. Items below cite file:line
and are grouped by tier. Execute top-down; commit each independently.

## Tier 0 — runtime deadlocks reachable today

- [x] **CONC-1** Nested-CALL semaphore deadlock — **not live; reclassified** (analysis 2026-05-26, commit `3ff1328`). The premise was that `_IN_FLIGHT_SEMAPHORE` is held across a parent's children, so a nested call would re-acquire an already-exhausted semaphore. It isn't: `run_turn` acquires and releases the semaphore *within a single turn* (`channel.py:469,484` — `acquire()` then `release()` in the `finally`), and a child is only driven *after* the parent turn has returned its `op:call` event — so the parent thread holds no in-flight slot while its child runs (`driver.py:506-522` steps effects serially; the inline child drive at `driver.py:677` happens once `_run_turn` has already released). A mechanism-2 chain of depth D therefore holds exactly one in-flight slot at a time (the deepest active turn). Independently, parallel fan-out (`tasks=[…]` via the MCP `call` tool) executes in each forked child's **own** `mcp_server` process — the plugin is launched per-session over stdio (`plugin.json` `mcpServers.call`), so the module-global semaphore never spans nesting levels. No deadlock path exists. Residual concern is memory, not deadlock (see README note + CONC-4).
- [x] **CONC-2** `_RUN_POOL` worker-pool starvation — **not live; reclassified** (analysis 2026-05-26, commit `3ff1328`). `_RUN_POOL` is used *only* for the one flat fan-out per `Driver.run` (`driver.py:304-310`); a forked child's own parallel fan-out runs in that child's separate `mcp_server` process with its own pool (per-process stdio plugin, see CONC-1). The single in-process nested handoff (the `op:call` envelope → `_spawn_child`) drives its one child **inline on the caller's thread** (`driver.py:677`), never submitting back to the pool, so recursion cannot exhaust it. Siblings are independent, so queued submissions never wait on running ones (`driver.py:310` `cf.wait` over independent futures). No starvation path exists.
- [x] **CONC-3** `_propagate_up` thread-safe via Driver-level RLock — commit `c4c4956`.
- [ ] **CONC-4** (split from CONC-1/CONC-2, analysis 2026-05-26) Aggregate subprocess count is not globally bounded. Because concurrency caps are per-`mcp_server`-process and each nesting level is a separate process, `CALLSTACK_MAX_CONCURRENT_FORKS` bounds fan-out *per level*, not across the whole tree. A wide **and** deep tree can hold many `claude` subprocesses live at once (~0.5–2 GB RSS each), risking host memory pressure — not a deadlock, but a real resource ceiling. No fix planned beyond documentation (README parallel-nested note + Configuration knobs); revisit only if a global cross-process budget is wanted.

## Tier 1 — security holes worth fixing before any release

- [ ] **SEC-101** MCP boundary has no auth (FastMCP stdio); any reachable client can spawn `claude` with caller-controlled `permission_mode`. Defense-in-depth: validate `permission_mode` against an allowlist at MCP boundary, drop unknown values. `plugins/callstack/mcp_server.py:178,254`.
- [x] **SEC-102** Cap `len(tasks)` at MCP boundary — commit `5a790cf`.
- [x] **SEC-103** `CALLSTACK_MAX_DEPTH` clamped to defensive ceiling (32) in `env.max_depth` — commit `5a790cf`.
- [ ] **SEC-104** TOCTOU + symlink race in `_resolve_cwd` → spawn: resolve happens long before `Popen(..., cwd=...)`. Fix: resolve once, then `os.open(dir, O_DIRECTORY|O_NOFOLLOW)` and pass `fd` (or re-validate just before spawn under a lock). `plugins/callstack/mcp_server.py:113-159` → `channel.py:591-594`.
- [ ] **SEC-105** Sensitive-prefix list is incomplete (missing `~/Library`, `~/Documents`, `~/Desktop`, `/private/tmp`, `~/.claude` itself); also the parent-project-under-prefix bypass opens the entire prefix tree. `plugins/callstack/mcp_server.py:106-110,148-152`.
- [x] **SEC-106** Strip ASCII control chars from `_one_line` so progress.log can't be hijacked by ANSI escapes in caller-/LLM-controlled strings — commit `20bc90d`.

## Tier 2 — correctness bugs that won't fire under happy-path tests

- [x] **CORR-101** Stamp `ENV_MAX_DEPTH` on every spawn — commit `3c0b969`.
- [x] **CORR-102** `parse_envelope` rejects mixed-opcode fenced blocks — commit `f613bd8`.
- [x] **CORR-103** Resume token has no nonce/version. — **skipped on second look**: the exploit path the review described ("stale tree snapshot + fresh resume = double-submit") is already closed by SEC-007's atomic-claim pattern in `TreeStore.load` (`trace.py:87-108`). The first resume `os.replace`s the snapshot to a unique claim file before reading; a replayed token sees `FileNotFoundError` and the MCP `resume` returns "Cannot find session file". A nonce would only re-protect what SEC-007 already protects. If a future code path bypasses `TreeStore.load`, revisit.
- [x] **CORR-104** Per-future exception isolation in `call_many` — commit `addf42e`.
- [ ] **CORR-105** No SIGINT handler: Ctrl-C leaves worker threads and pooled subprocesses running. Install a handler in `Driver.run` that cancels futures and tears down the pool.

## Tier 3 — performance hot-paths

- [x] **PERF-101** Hash-skip ignores `ended_at` so quiet ticks short-circuit — commit `04d9f72`.
- [x] **PERF-102** YAML emit held under `_interprocess_lock`. — **skipped**: after PERF-101 landed the hash-skip actually triggers on quiet ticks, so `yaml.safe_dump` no longer runs on every merge. The remaining "move dump out of lock" optimization introduces TOCTOU (process B could publish a newer report between our hash-check and our atomic-write) for marginal wall-clock savings under contention. Worth revisiting only if a perf profile shows the dump dominating with the hash-skip fix in place.
- [x] **PERF-103** `_FRAMES_PARSED_CACHE` bounded with LRU eviction — commit `4dcb186`.
- [x] **PERF-104** Dir-mtime fast-path in `_load_frames` skips re-glob on quiet ticks — commit `4dcb186`.
- [x] **PERF-105** `_evict_excess_locked` sorts once instead of per-pop — commit `70302ed`.
- [ ] **PERF-106** No backpressure on `on_progress`: a fast child fires 100s/sec; transition log + frame write are synchronous. Coalesce by debounce window. `plugins/callstack/agent_callstack/driver.py:326`.

## Tier 4 — DRY / simplicity wins

- [x] **DRY-101** Extract `env.py` with all `CALLSTACK_*` env constants + typed readers — commit `3ddaf66`.
- [x] **DRY-102** Stale `CALLSTACK_ROOT_*` env cleared after rejection so Caller agrees — commit `7bfefc5`.
- [ ] **DRY-103** Delete three replicas of Claude Code's session internals now that `--session-id` preallocation is authoritative: `_extract_cwd`, `_load/_save_session_index`, `count_lines`. `plugins/callstack/agent_callstack/session.py:288,308-352,354`. — **deferred**: each target is still load-bearing on at least one path. `_extract_cwd` powers `SessionRef.cwd` which `Driver._derive_call_type` (driver.py:327) uses for cross-project detection. `count_lines` is the fallback for `parent_lines` when the precise `## Starting Task` marker scan misses (results.py:131). `_load/_save_session_index` is the fast-path for resolving sessions callstack didn't spawn (user's top-level interactive claude). The reviewer's claim that `--session-id` preallocation makes these deletable is too optimistic — strip them and the user-interactive path slows to a full project-dir scan on every miss. Tackle as a separate refactor that simultaneously moves `SessionRef.cwd` to an explicit field at every construction site. — **deferred**: each target is still load-bearing on at least one path. `_extract_cwd` powers `SessionRef.cwd` which `Driver._derive_call_type` (driver.py:327) uses for cross-project detection. `count_lines` is the fallback for `parent_lines` when the precise `## Starting Task` marker scan misses (results.py:131). `_load/_save_session_index` is the fast-path for resolving sessions callstack didn't spawn (user's top-level interactive claude). The reviewer's claim that `--session-id` preallocation makes these deletable is too optimistic — strip them and the user-interactive path slows to a full project-dir scan on every miss. Tackle as a separate refactor that simultaneously moves `SessionRef.cwd` to an explicit field at every construction site.
- [ ] **DRY-104** Consolidate six DFS variants behind `_TreeIndex`: `_depth_of`, `_parent_file_for`, `_find_parent`, `_find`, `Tree.find_by_session`, `_chain_to_session`, `_walk_tree`. `plugins/callstack/agent_callstack/driver.py:355,370,418,730,191`, `frames.py:288,227`.
- [x] **DRY-105** Trivial dedups: UUID regex, `_utc_now`, drop `_wrap` identity, inline `_stringify` — commit `5fe8f35`. Atomic-write dedup skipped (different error contracts).
- [ ] **DRY-106** Pick one error-logging policy: first-occurrence-only / always / silent are all in use across modules. Standardize on "log first occurrence per source, suppress repeats."

---

# Thermo-nuclear review — 2026-05-22

From the in-flight diff covering shutdown-hardening / orphan-reconciliation / pre-finalize wait /
`run_in_background`. Reviewer flagged five overlapping "seal non-terminal nodes" mechanisms that
should collapse to one policy; signal/atexit cross-cutting concerns leaked into `_LiveReporter`;
`_finalize_own_frames` running in happy-path `finally`; PID-reuse hazard in `_pid_alive`; and
a depth-budget policy bump that wasn't justified. Work top-down.

## Tier 0 — structural cleanups

- [ ] **REVIEW-201** Collapse the five abandonment paths to one policy + two thin walkers.
  Today `frames._abandon_non_terminal_nodes`, `reporter._abandon_tree_nodes_in_place`,
  `reporter._abandon_frame_nodes_in_place`, `wait_for_terminal_signals` (Timeout variant),
  and `_emergency_finalize_on_shutdown` each open-code the "find non-terminal nodes, rewrite
  to synthetic terminal kind" logic. Define a single `mark_abandoned(kind, *, reason, sid)`
  policy in `state.py`; expose two walkers (Tree and frame-dict) that delegate to it.
  Side-effect bug to fix in the same pass: `frames._abandon_non_terminal_nodes` rewrites
  `AwaitingUser` nodes (it only consults `_TERMINAL_KINDS`, not `SUSPENDED`), demoting
  legitimately-yielded calls to `abandoned`.

- [ ] **REVIEW-202** Lift atexit/signal-handler installation out of `_LiveReporter` into a
  dedicated `shutdown.py` (or fold into `_InvocationContext` startup). `_LiveReporter.__init__`
  is called from `asyncio.to_thread` in the MCP server — i.e. NOT on the main thread — so
  `_install_shutdown_hooks` silently skips signal installation; fix #3 is currently a
  partial no-op in production. Verify with a regression test that signal hooks are installed
  at process startup, not at reporter construction.

- [ ] **REVIEW-203** Move `_finalize_own_frames` from the happy-path `finally` into an `except`
  branch in `mcp_server.call` and `mcp_server.await_call`. Extract the three boundary callsites
  into one `@contextmanager` (`_finalize_at_boundary`) so the pattern is identical and the
  no-op cost on success drops to zero (today every tool call globs the frames dir, takes the
  fcntl lock, and parses every frame).

## Tier 1 — correctness

- [ ] **REVIEW-204** Add wall-clock TTL alongside `_pid_alive`. PID reuse on macOS cycles
  within a few thousand spawns, so a frame whose writer died and whose PID was reclaimed
  by an unrelated process stays "alive-looking" forever — defeating the reconciliation fix.
  Resolution: a frame is abandoned-eligible if `_pid_alive(writer_pid)` is False OR
  `(now - frame.started_at) > 2 * MCP_TOOL_TIMEOUT` (default).

- [ ] **REVIEW-205** `_load_frames` cache contract silently broke: `_reconcile_orphan_states`
  now mutates the inner frame dicts that are *shared* between the cache and callers. The
  outer docstring still says callers can mutate freely. Either deep-copy frame dicts on each
  cache hit (the YAML-parse skip already dominates the cost) or document the sharper
  "caller may mutate top-level dict + list, not contents" contract at the function header.

## Tier 2 — simplicity / spaghetti

- [ ] **REVIEW-206** `driver._classify_upstream_failure` is a table-driven dispatcher wrapping
  a one-entry tuple. Inline back to a direct `if "API Error" in text and "Server is temporarily
  limiting requests" in text` until a second signature appears.

- [ ] **REVIEW-207** Justify or back out the `MAX_DEPTH` default 10 → 64 and ceiling 32 → 128
  bumps in `env.py`. Combined with default fanout 64 these are a 4× ceiling jump with no
  cited workflow. If the bump exists for a specific use case, document it in `env.py` with
  the workflow name and the observed depth; otherwise revert to a smaller increment.

- [ ] **REVIEW-208** Boundary leak — `Caller.run` reaches into `Driver._current_tree`. Promote
  to public `Driver.last_tree` (or restructure the fallback path so `Caller` doesn't need
  it). Minor compared to the items above.
