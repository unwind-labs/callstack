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

- [ ] **CONC-1** Nested-CALL semaphore deadlock: `_IN_FLIGHT_SEMAPHORE` is held for a parent's entire turn; if that turn issues `/call`, the child needs the same module-global semaphore. 16 parents all calling deadlock the host. `plugins/callstack/agent_callstack/channel.py:114-119,463`. Fix: detect nested-CALL via a thread-local depth counter and release+reacquire (or use a separate semaphore for forked children).
- [ ] **CONC-2** `_RUN_POOL` worker-pool starvation: module-level `ThreadPoolExecutor` is shared across nested `call_many` invocations; recursive fan-out exhausts it, parents wait on children that can't get a slot. `plugins/callstack/agent_callstack/driver.py:46-67,271-283`. Fix: detect "nested" submissions and run inline on the caller's thread, or use a per-depth pool.
- [ ] **CONC-3** `_propagate_up` is not thread-safe under sibling `_drive` calls; mutates ancestor `Node.state` / `children` without a lock. `plugins/callstack/agent_callstack/driver.py:388-416,271-277`. Fix: tree-level `threading.Lock` around the propagate path.

## Tier 1 — security holes worth fixing before any release

- [ ] **SEC-101** MCP boundary has no auth (FastMCP stdio); any reachable client can spawn `claude` with caller-controlled `permission_mode`. Defense-in-depth: validate `permission_mode` against an allowlist at MCP boundary, drop unknown values. `plugins/callstack/mcp_server.py:178,254`.
- [ ] **SEC-102** Cap `len(tasks)` at MCP boundary (currently unbounded — combined with 0.5–2 GB RSS per child, one MCP call can OOM). `plugins/callstack/mcp_server.py:178`. Suggested cap: 64 with env override `CALLSTACK_MAX_FANOUT`.
- [ ] **SEC-103** `CALLSTACK_MAX_DEPTH` is read from caller env, so a caller raises its own cap. Move to a config that's stamped onto spawned children and rejects increases past the root's cap. `plugins/callstack/agent_callstack/__init__.py:91-102,303-306`.
- [ ] **SEC-104** TOCTOU + symlink race in `_resolve_cwd` → spawn: resolve happens long before `Popen(..., cwd=...)`. Fix: resolve once, then `os.open(dir, O_DIRECTORY|O_NOFOLLOW)` and pass `fd` (or re-validate just before spawn under a lock). `plugins/callstack/mcp_server.py:113-159` → `channel.py:591-594`.
- [ ] **SEC-105** Sensitive-prefix list is incomplete (missing `~/Library`, `~/Documents`, `~/Desktop`, `/private/tmp`, `~/.claude` itself); also the parent-project-under-prefix bypass opens the entire prefix tree. `plugins/callstack/mcp_server.py:106-110,148-152`.
- [ ] **SEC-106** YAML/JSON log injection: caller-controlled task strings and LLM-controlled child output flow verbatim into `report.yaml`. Use `yaml.safe_dump(default_style="|")` for arbitrary strings, or truncate + sanitize control chars. `plugins/callstack/agent_callstack/driver.py:588-598`.

## Tier 2 — correctness bugs that won't fire under happy-path tests

- [ ] **CORR-101** `max_depth` not stamped onto spawn env; grandchild reverts to default. Stamp `ENV_MAX_DEPTH` on every spawn alongside `ENV_DEPTH`. `plugins/callstack/agent_callstack/__init__.py:303-306`.
- [ ] **CORR-102** `_last_json_object` prefers the LAST fenced block: a child that emits real YIELD then fake RETURN in plaintext bypasses yielding. Fix: refuse parse when both YIELD and RETURN appear; prefer first valid envelope; or reject when more than one is found. `plugins/callstack/agent_callstack/protocol.py:55-84`.
- [ ] **CORR-103** Resume token has no nonce/version; replay reachable from a stale tree snapshot. Add `(invoke_id, leaf_node_id, version)` tuple to `YieldToken` and verify on resume. `plugins/callstack/agent_callstack/driver.py:303-322`.
- [ ] **CORR-104** Partial-failure swallowed in `call_many`: first raised exception aborts the loop, siblings' results not collected. Wrap each `fut.result()` in try/except and surface as `CallFailed` for that slot. `plugins/callstack/agent_callstack/driver.py:266-283`.
- [ ] **CORR-105** No SIGINT handler: Ctrl-C leaves worker threads and pooled subprocesses running. Install a handler in `Driver.run` that cancels futures and tears down the pool.

## Tier 3 — performance hot-paths

- [ ] **PERF-101** Hash-skip in `_do_merge` is dead: `ended_at` always perturbs the doc so the content hash never matches. Hash the doc WITHOUT `ended_at`, or drop `ended_at` from quiet ticks. **Biggest single CPU win.** `plugins/callstack/agent_callstack/reporter.py:189-212`.
- [ ] **PERF-102** YAML emit held under `_interprocess_lock`. Move serialization outside the lock; lock only around the atomic rename. `plugins/callstack/agent_callstack/reporter.py:189-212`.
- [ ] **PERF-103** Module-level caches (`_FRAMES_PARSED_CACHE`, `_mru_cache`, `_SHARED_LOCATOR`) never evict. Long-lived MCP server → unbounded RSS. Add size-bounded LRU. `plugins/callstack/agent_callstack/frames.py:39,309`, `session.py:80`.
- [ ] **PERF-104** `_load_frames` re-globs every tick; with `instance_id=uuid4` per nested invoke, frame count grows unboundedly *within one invocation*. Stat-based fast-path: skip glob when dir mtime unchanged. `plugins/callstack/agent_callstack/reporter.py:194`.
- [ ] **PERF-105** Per-eviction `sorted(processes.items())` is O(n log n) per evicted entry. Use a heap or pre-sort once. `plugins/callstack/agent_callstack/channel.py:347`.
- [ ] **PERF-106** No backpressure on `on_progress`: a fast child fires 100s/sec; transition log + frame write are synchronous. Coalesce by debounce window. `plugins/callstack/agent_callstack/driver.py:326`.

## Tier 4 — DRY / simplicity wins

- [ ] **DRY-101** Extract `env.py` with all `CALLSTACK_*` env constants + typed readers (`root_identity()`, `debounce_secs()`, `max_depth()`). Eliminates duplicated string keys across `__init__.py`, `session.py`, `channel.py`, `reporter.py`, `mcp_server.py`, `frames.py`. **Highest-leverage cleanup.**
- [ ] **DRY-102** Collapse `mcp_server._invocation_identity` + `_report_path` into `_InvocationContext`. ~60 LOC removed, one source of drift gone. `plugins/callstack/mcp_server.py:47,51` ↔ `agent_callstack/__init__.py:249`.
- [ ] **DRY-103** Delete three replicas of Claude Code's session internals now that `--session-id` preallocation is authoritative: `_extract_cwd`, `_load/_save_session_index`, `count_lines`. `plugins/callstack/agent_callstack/session.py:288,308-352,354`. Matches standing memory note "don't replicate Claude Code internals".
- [ ] **DRY-104** Consolidate six DFS variants behind `_TreeIndex`: `_depth_of`, `_parent_file_for`, `_find_parent`, `_find`, `Tree.find_by_session`, `_chain_to_session`, `_walk_tree`. `plugins/callstack/agent_callstack/driver.py:355,370,418,730,191`, `frames.py:288,227`.
- [ ] **DRY-105** Trivial dedups: UUID regex (`session.py:22-25` ↔ `channel.py:133-136`), `_utc_now` (`driver.py:26` ↔ `invocation_ctx.py:28`), atomic-write (`reporter.py:365` ↔ `session.py:332`); delete `_wrap` identity (`results.py:165`); inline `_stringify` (`protocol.py:123`).
- [ ] **DRY-106** Pick one error-logging policy: first-occurrence-only / always / silent are all in use across modules. Standardize on "log first occurrence per source, suppress repeats."
