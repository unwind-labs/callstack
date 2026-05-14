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
