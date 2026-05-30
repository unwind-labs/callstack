# RFC: Deepen the merge pipeline

_one `MergeEngine` owning frames → document → disk; pure reconcile/graft/hash logic; a `Liveness` seam to end the monkeypatching_

Labels: enhancement

---

## Problem

`report.py` advertises `InvocationReport` as a "deep facade," and as a path/identity boundary it largely is. But the **merge pipeline** — the thing that turns `_frames/*.yaml` into a merged `report.yaml` — is owned by no single module. It is a sequence:

> glob frame files → parse (2-level LRU cache) → orphan-reconcile dead writers → graft nested trees under matching caller nodes → compute overall status → serialize YAML → atomic-write, with a content-hash write-skip and a cross-process lock

…and it is **split across three files and invoked from two-to-three call sites that partly duplicate it**:

- `report.merged_document()` (report.py:178) → `_load_frames` + `_build_merged_report` (both in `frames.py`); `write_report` (report.py:203) then serializes + atomic-writes — **without taking the cross-process lock** (a latent race against the live reporter).
- `reporter._do_merge()` (reporter.py:200) → the *same* `_load_frames` + `_build_merged_report`, then **re-implements** serialize + atomic-write inline, plus the hash-skip (`_content_hash_ignoring_ended_at`), the `_interprocess_lock`, and the "no root frame yet → skip" guard.
- `reporter._write_partial_if_no_root()` (reporter.py:235) is a **third** partial re-implementation of serialize + write.

So "how is a merged report produced?" has no single answer. The serialize-and-atomically-write step exists in three spellings; the no-root-frame guard in two; only one of the three holds the lock.

### The shallow sub-module inside the deep one

`_load_frames(frames_dir) -> dict[str, list[dict]]` (frames.py:228) is a 3-word signature hiding 116 lines doing **four** unrelated jobs: filesystem I/O (glob/stat/parse), a two-level stat-keyed LRU cache (`_FRAMES_PARSED_CACHE` + `_FRAMES_DIR_CACHE`), orphan reconciliation (PID-liveness + TTL state rewrites), and error tolerance (oversized/malformed files, key-shape validation). It also carries an implicit "caller owns the returned dict" contract enforced only by `copy.deepcopy` on every cache hit.

### The testability tax

The genuinely-testable logic is entangled with impure I/O, so:

- Orphan reconciliation can only be tested by laying down real frame files **and** `monkeypatch.setattr(frames, "_pid_alive", …)` and `monkeypatch.setattr(frames, "read_orphan_ttl_seconds", …)` — reaching into module privates.
- The content-hash write-skip can only be verified by stat-ing `report.yaml`'s mtime to infer whether a write happened.

This is exactly the friction the deep-module thesis targets: the place bugs live (reconciliation policy, hash-skip, no-root guard) is the place that is hardest to test.

## Proposed Interface

A **C+D hybrid** (of four designs explored): Design C's stateful `MergeEngine` as the orchestration spine, Design D's separation of pure logic from impure boundaries — but only for the boundary that actually pays off in testability (liveness + clock + TTL), keeping the lock/atomic-write as real-file operations.

### `MergeEngine` — owns the whole frames → document → disk pipeline

```python
@dataclass(frozen=True)
class MergeResult:
    wrote: bool                      # did bytes hit report.yaml this call?
    skipped_reason: Optional[str]    # "no-root-frame" | "hash-unchanged" | None
    document: Optional[dict]         # the merged doc (None iff no root frame yet)
    path: Optional[Path]             # report.yaml path when wrote is True
    new_hash: Optional[bytes]        # content hash (ignoring ended_at) of the doc

@dataclass(frozen=True)
class OrphanPolicy:
    ttl_seconds: float               # resolved from env AT THE EDGE, passed in

class MergeEngine:
    """The single boundary over frames.py's load/merge and reporter.py's
    serialize/hash-skip/lock/atomic-write. One engine per reporter so it can
    own the per-tick hash-skip state; cold callers build a throwaway one."""

    def __init__(self, ctx: _InvocationContext, *,
                 store: Optional[FileStore] = None,
                 liveness: Liveness = _OsLiveness()) -> None: ...

    # ---- HOT path: one call, default args tuned for the per-tick caller ----
    def merge_to_disk(self, *, ended_at: str, force: bool = False,
                      policy: OrphanPolicy) -> MergeResult:
        """Under the cross-process lock: load + reconcile + (no-root guard) +
        build + hash-skip (unless force) + atomic-write. Advances the engine's
        owned hash on a successful write. The ONLY writer of report.yaml."""

    # ---- COLD path: pure read, no lock, no write, no hash state ----
    def build_document(self, *, ended_at: str, policy: OrphanPolicy) -> Optional[dict]:
        """Merged doc from current frames, or None when no root frame exists."""

    def load_frames(self, *, policy: OrphanPolicy) -> dict[str, list[dict]]:
        """store.read_frames + reconcile_orphans; caller-owned, mutation-safe."""

    def write_partial_if_no_root(self, tree_dict: dict, *, ended_at: str,
                                 kind: str, started_at: str) -> Optional[Path]:
        """Nested fallback: under the lock, if report.yaml absent and no root
        frame, write report.partial.yaml. Shares the lock + serialize path."""

    # ---- PURE logic, exposed for direct unit test (NO I/O, NO env) ----
    def reconcile_orphans(self, frames: dict[str, list[dict]], *,
                          policy: OrphanPolicy) -> int:
        """Promote eligible non-terminal nodes in dead-writer frames to
        'abandoned'. Uses self._liveness (pid_alive + now). Idempotent.
        Takes ONE now() at the top, not per-frame. Returns count mutated."""

    @staticmethod
    def build_merged_report(*, invoke_id: str, frames: dict[str, list[dict]],
                            root_frame: dict, ended_at: str) -> dict: ...

    @staticmethod
    def content_hash_ignoring_ended_at(doc: dict) -> bytes: ...
```

### `FileStore` — the single filesystem dependency (NOT a fully in-memory port)

```python
class FileStore:
    """Owns ALL frame-file + report-byte I/O for one invocation. The dir-mtime
    fast path + parsed-frame LRU live INSIDE here; read_frames deep-copies
    before returning (mutation-safety contract). Construct from ctx; tests
    point ctx.log_dir at tmp_path and use the REAL store."""
    def __init__(self, ctx: _InvocationContext) -> None: ...
    def read_frames(self) -> dict[str, list[dict]]: ...          # glob/parse/cache/tolerate
    @contextlib.contextmanager
    def merge_lock(self) -> Iterator[None]: ...                   # fcntl, 30s, proceed-on-timeout
    def write_atomic(self, path: Path, payload: bytes) -> None: ...  # tmp + fsync + os.replace
    def report_exists(self, report_path: Path) -> bool: ...
```

### `Liveness` — the cheap, high-value injected port

```python
class Liveness(Protocol):
    def pid_alive(self, pid: int) -> bool: ...    # os.kill(pid, 0) trichotomy
    def now(self) -> float: ...                   # time.time()
# Production: _OsLiveness. Tests: FakeLiveness(alive: set[int], clock: float).
```

### Usage — the duplication collapsing

**Hot path** (`_LiveReporter._do_merge`, ~25 lines → 4):

```python
def _do_merge(self, *, force: bool, ended_at: str) -> None:
    res = self._engine.merge_to_disk(
        ended_at=ended_at, force=force,
        policy=OrphanPolicy(ttl_seconds=read_orphan_ttl_seconds()),
    )
    if res.wrote:
        self._last_merged_hash = res.new_hash   # or let the engine own it entirely
```

(The engine owns the hash internally; `_LiveReporter` drops its own `_last_merged_hash` field.)

**Cold path** (`report.merged_document` / `write_report`):

```python
def merged_document(self, *, ended_at=None) -> Optional[dict]:
    ts = _utc_now_iso() if ended_at is None else ended_at
    return MergeEngine(self._ctx).build_document(ended_at=ts, policy=_default_policy())

def write_report(self, *, ended_at=None) -> Optional[Path]:
    ts = _utc_now_iso() if ended_at is None else ended_at
    return MergeEngine(self._ctx).merge_to_disk(ended_at=ts, force=True,
                                                policy=_default_policy()).path
    # NB: now takes the cross-process lock — closes the write_report race.
```

### What complexity it hides

`report.py` and `reporter.py` stop knowing about lock files, content hashes, YAML dump params (`sort_keys=False, width=120, allow_unicode=True` — duplicated in three places today), atomic-write tmpfiles, `_ROOT_FRAME_KEY`, and the no-root guard. The `_load_frames` four jobs hide behind `FileStore.read_frames`. Reconciliation policy, graft, status rollup, and the `ended_at`-stripped hash become pure engine methods that touch neither the clock, the filesystem, nor the env.

## Dependency Strategy

**Mixed, by design — split the boundaries by whether faking them is honest:**

- **`Liveness` (PID probe + clock) → Ports & adapters.** Trivially fakeable, and the monkeypatching pain is real and recurring. Production `_OsLiveness` (`os.kill(pid, 0)` + `time.time()`); test `FakeLiveness(alive, clock)`. TTL is `OrphanPolicy` data resolved at the edge (`read_orphan_ttl_seconds()`), not an env read inside the engine — this is what kills `monkeypatch.setattr(frames, "read_orphan_ttl_seconds", …)`.
- **`FileStore` (glob/parse/cache, `fcntl` lock, `os.replace` atomic write) → Local-substitutable, real files under `tmp_path`.** Deliberately **not** a fully in-memory port: faking `fcntl.lockf` / `os.replace` / `st_mtime_ns` would re-implement POSIX semantics — exactly the bug surface we want to test — so a fake would test the fake. Tests point `ctx.log_dir` at `tmp_path` and use the real store. The caches stay process-global inside `FileStore`/`frames.py` so the dir-mtime fast path survives across engine instances.

## Testing Strategy

**New boundary tests (pure, no I/O, no monkeypatching):**
- `reconcile_orphans`: dead writer (pid not in `FakeLiveness.alive`) → node promoted to `abandoned`; live + young → untouched; **PID-reuse defeated by TTL** (pid reads alive but frame is ancient → abandoned). All via injected `FakeLiveness` + `OrphanPolicy`, zero files, zero `monkeypatch`.
- Single-`now()`: assert `reconcile_orphans` calls `liveness.now()` exactly once regardless of frame count (guards the moving-clock ordering risk).
- `content_hash_ignoring_ended_at`: two docs differing only in `ended_at` hash equal; any other field change differs.

**New behavioral tests (real files under `tmp_path`, asserting `MergeResult`):**
- Hash-skip: `merge_to_disk` → `wrote=True`; same frames, advanced `ended_at` → `wrote=False, skipped_reason="hash-unchanged"` **and `report.yaml` mtime unchanged** (Rule 9: fails if skip returns `wrote=False` but still writes); mutate a frame → `wrote=True`.
- No-root guard: only a nested frame on disk → `wrote=False, skipped_reason="no-root-frame"`, no `report.yaml`; then land a root frame → written.
- Partial fallback: nested finalize with no root → `report.partial.yaml` with `status="partial"`; root lands → partial yields.
- Cross-process lock: hold `ctx.lock_path` from a second thread/process, assert `merge_to_disk` serializes / proceeds-after-timeout and the YAML round-trips uncorrupted.
- `write_report` now acquires the lock (regression test for the race it skips today).

**Old tests to delete / migrate:**
- `tests/test_orphan_reconciliation.py` cases that `monkeypatch.setattr(frames, "_pid_alive", …)` → rewrite against `FakeLiveness`.
- Hash-skip tests that infer writes from mtime → assert on `MergeResult.wrote`.
- `_load_frames`-internal cache tests stay as white-box `FileStore` unit tests where the optimization has no behavioral expression.

**Test environment needs:** none beyond `tmp_path`. `FakeLiveness` is pure in-memory. A shared adapter-contract test should run the mutation-safety assertion against the real `FileStore` (the deep-copy contract must not silently differ from any fake).

## Implementation Recommendations

Durable guidance, decoupled from current file paths:

- **One module owns "frames on disk → merged document → report.yaml on disk."** It is the only writer of `report.yaml`, so the cross-process lock and atomic write cannot be bypassed by a caller (the current `write_report`-skips-the-lock race is exactly this failure).
- **Per-writer state (the content-hash skip) belongs to the per-writer engine,** not threaded through a reporter field. The skip must be observable as a return value (`wrote`), not inferable from file mtimes.
- **Separate pure logic from impure boundaries — but only port the boundaries where faking is honest.** Liveness/clock/TTL are cheap to fake and the test value is high; port them and pass TTL as data. The filesystem lock + atomic write encode POSIX semantics; exercise them on real files, because a fake would test itself.
- **Orphan reconciliation must run on every load, including cache hits,** and must read the clock once per reconciliation pass, not once per frame.
- **Keep the frame caches process-global** so the dir-mtime fast path survives across engine instances; the engine owns only the write-skip hash.
- **Deferred (skip): a fully pluggable source/sink/shape pipeline.** Four extension protocols for two real callers is speculative (Rule 2). One contained cleanup is optional — folding the two parallel graft functions (report-shape `_graft_node` and raw-shape `_graft_raw`, which already share `_grafted_children`) into a single shape-parameterized walker. Nice, not load-bearing.
- **Out of scope but adjacent:** `_finalize_own_frames` also does glob + parse + own-pid-filter + lock + write. Either route its reads through the same `FileStore` or accept the asymmetry explicitly — don't leave it as a silent second I/O path.

---

*Filed via the `improve-codebase-architecture` workflow (Candidate 2 of 5: the merged-report pipeline). Recommendation is a hybrid of four independently-designed interfaces.*
