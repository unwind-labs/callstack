"""Tests for the ClaudePool process-per-session reuse (PERF-D).

Two layers:

1. ClaudePool unit tests — exercise acquire/register/evict/LRU directly
   with mock _PooledProcess objects. No subprocess.Popen involved.

2. ClaudeChannel integration tests — patch ClaudeChannel._spawn and
   ._run_one_turn to count spawns and verify the pool's reuse logic
   without needing the real `claude` CLI.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest
from agent_callstack import channel_pool as pool_mod
from agent_callstack.channel import ClaudeChannel, TurnResult, TurnTimeout
from agent_callstack.channel_pool import ClaudePool, _get_pool, _PooledProcess, shutdown_pool

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


class _FakeClock:
    """Deterministic monotonic clock: each read advances by a fixed tick, so
    successive pool register/acquire calls get strictly increasing `last_used`
    timestamps. T-M2: replaces real time.sleep() gaps, which on a loaded CI
    host may not advance time.monotonic() enough to guarantee LRU ordering,
    making which entry counts as 'least recently used' nondeterministic."""

    def __init__(self, start: float = 0.0, tick: float = 1.0):
        self._now = start
        self._tick = tick

    def __call__(self) -> float:
        self._now += self._tick
        return self._now


def _mock_pooled_process() -> _PooledProcess:
    """Build a _PooledProcess wrapping mocks that look alive."""
    proc = MagicMock()
    proc.poll.return_value = None  # alive
    proc.returncode = None
    proc.wait.return_value = 0
    entry = _PooledProcess(
        proc=proc,
        stdin=MagicMock(),
        stdout=MagicMock(),
        log=MagicMock(),
        log_path="/tmp/mock.log",
        cwd="/tmp",
    )
    return entry


@pytest.fixture
def pool() -> ClaudePool:
    return ClaudePool(max_size=4)


@pytest.fixture
def fresh_module_pool():
    """Swap out the module-level pool so tests can't leak into each other."""
    saved = pool_mod._pool
    pool_mod._pool = ClaudePool(max_size=pool_mod._MAX_CONCURRENT_FORKS)
    try:
        yield pool_mod._pool
    finally:
        pool_mod._pool.shutdown()
        pool_mod._pool = saved


# --------------------------------------------------------------------------
# 1. ClaudePool unit tests
# --------------------------------------------------------------------------


class TestClaudePool:
    def test_register_then_acquire_returns_same_entry(self, pool):
        p = _mock_pooled_process()
        pool.register("00000000-0000-0000-0000-0000000000c3", p)
        assert pool.acquire("00000000-0000-0000-0000-0000000000c3") is p

    def test_acquire_missing_returns_none(self, pool):
        assert pool.acquire("nope") is None

    def test_dead_process_is_dropped_on_acquire(self, pool):
        p = _mock_pooled_process()
        p.proc.poll.return_value = 1  # exited
        pool.register("00000000-0000-0000-0000-0000000000c3", p)
        assert pool.acquire("00000000-0000-0000-0000-0000000000c3") is None
        # Subsequent acquire still None (entry was cleaned).
        assert pool.acquire("00000000-0000-0000-0000-0000000000c3") is None

    def test_evict_removes_and_closes(self, pool):
        p = _mock_pooled_process()
        pool.register("00000000-0000-0000-0000-0000000000c3", p)
        pool.evict("00000000-0000-0000-0000-0000000000c3")
        assert pool.acquire("00000000-0000-0000-0000-0000000000c3") is None
        p.proc.terminate.assert_called()

    def test_lru_eviction_when_over_cap(self):
        # _FakeClock gives each register a strictly increasing last_used, so
        # s1 is unambiguously the LRU victim (T-M2: no real sleeps).
        pool = ClaudePool(max_size=2, clock=_FakeClock())
        p1 = _mock_pooled_process()
        p2 = _mock_pooled_process()
        p3 = _mock_pooled_process()
        pool.register("s1", p1)
        pool.register("s2", p2)
        pool.register("s3", p3)  # over cap → evict the LRU (s1)
        assert pool.size() == 2
        assert pool.acquire("s1") is None
        assert pool.acquire("s2") is p2
        assert pool.acquire("s3") is p3
        # The evicted process was terminated.
        p1.proc.terminate.assert_called()

    def test_lru_eviction_handles_multiple_excess_entries(self):
        """PERF-105: the loop sorts once and pops several entries in a row.
        Verify multi-entry eviction still picks the oldest first."""
        pool = ClaudePool(max_size=2, clock=_FakeClock())
        entries: list = []
        for i in range(6):
            p = _mock_pooled_process()
            entries.append(p)
            pool.register(f"s{i}", p)
        # Only the last two registered (s4, s5) should survive.
        assert pool.size() == 2
        assert pool.acquire("s5") is entries[5]
        assert pool.acquire("s4") is entries[4]
        for i in range(4):
            assert pool.acquire(f"s{i}") is None, f"expected s{i} to be evicted as LRU but it survived"
            entries[i].proc.terminate.assert_called()

    def test_in_use_entries_are_skipped_during_eviction(self):
        pool = ClaudePool(max_size=1)
        p1 = _mock_pooled_process()
        p2 = _mock_pooled_process()
        pool.register("s1", p1)
        # Simulate p1 being held by an in-flight turn.
        p1.lock.acquire()
        try:
            pool.register("s2", p2)
            # p1 is busy so eviction couldn't reclaim it; pool exceeds cap.
            assert pool.size() == 2
            assert pool.acquire("s1") is p1
            assert pool.acquire("s2") is p2
        finally:
            p1.lock.release()

    def test_re_register_replaces_old_entry(self, pool):
        p1 = _mock_pooled_process()
        p2 = _mock_pooled_process()
        pool.register("00000000-0000-0000-0000-0000000000c3", p1)
        pool.register("00000000-0000-0000-0000-0000000000c3", p2)
        assert pool.acquire("00000000-0000-0000-0000-0000000000c3") is p2
        # The displaced process was closed.
        p1.proc.terminate.assert_called()

    def test_shutdown_closes_every_entry(self, pool):
        p1 = _mock_pooled_process()
        p2 = _mock_pooled_process()
        pool.register("s1", p1)
        pool.register("s2", p2)
        pool.shutdown()
        assert pool.size() == 0
        p1.proc.terminate.assert_called()
        p2.proc.terminate.assert_called()


# --------------------------------------------------------------------------
# 2. ClaudeChannel integration tests (mocked spawn/turn)
# --------------------------------------------------------------------------


def _make_turn_result(session_id: str) -> TurnResult:
    return TurnResult(
        text='```json\n{"op": "return", "result": "ok"}\n```',
        session_id=session_id,
        duration=0.01,
        api_request_id="",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        total_cost_usd=0.0,
    )


class _SpawnAndTurnRecorder:
    """Replacement for ClaudeChannel._spawn + _run_one_turn that records
    spawn calls and emits canned TurnResults. session_id chosen per spawn."""

    def __init__(self, session_ids: list[str]):
        self._session_ids = list(session_ids)
        self.spawn_calls: list[tuple[str, str]] = []  # (source_sid, mode)
        self.turn_calls: list[tuple[str, str, bool]] = []  # (sid, prompt, do_handshake)
        self._lock = threading.Lock()

    def patch(self, monkeypatch, channel: ClaudeChannel) -> None:
        recorder = self

        def fake_spawn(self_, source_sid, mode, cwd, extra_env, *, preallocated_session_id=None):
            with recorder._lock:
                recorder.spawn_calls.append((source_sid, mode))
                # Each spawn binds to the next prepared session_id.
                next_sid = recorder._session_ids.pop(0) if recorder._session_ids else source_sid
            entry = _mock_pooled_process()
            entry.session_id = next_sid  # pre-mark for _run_one_turn
            return entry

        def fake_run_one_turn(
            self_, entry, prompt, timeout, on_session_id, *, do_handshake, preallocated_session_id=None
        ):
            sid = entry.session_id or "auto-sid"
            with recorder._lock:
                recorder.turn_calls.append((sid, prompt, do_handshake))
            if on_session_id is not None:
                on_session_id(sid)
            entry.initialized = True
            entry.session_id = sid
            entry.last_used = time.monotonic()
            return _make_turn_result(sid)

        monkeypatch.setattr(ClaudeChannel, "_spawn", fake_spawn)
        monkeypatch.setattr(ClaudeChannel, "_run_one_turn", fake_run_one_turn)


class TestChannelPoolIntegration:
    def test_resume_reuses_same_pooled_process(self, monkeypatch, fresh_module_pool):
        rec = _SpawnAndTurnRecorder(session_ids=["00000000-0000-0000-0000-0000000000c2"])
        ch = ClaudeChannel()
        rec.patch(monkeypatch, ch)

        # First turn: fork. Spawns, ends with session_id=sX, pooled.
        r1 = ch.run_turn("00000000-0000-0000-0000-0000000000c0", "task1", mode="fork")
        assert r1.session_id == "00000000-0000-0000-0000-0000000000c2"
        assert len(rec.spawn_calls) == 1
        assert rec.turn_calls[0][2] is True  # do_handshake on first turn

        # Second turn: resume against sX. Should hit the pool — no new spawn.
        r2 = ch.run_turn("00000000-0000-0000-0000-0000000000c2", "task2", mode="resume")
        assert r2.session_id == "00000000-0000-0000-0000-0000000000c2"
        assert len(rec.spawn_calls) == 1, "second resume should reuse"
        assert rec.turn_calls[1][2] is False  # no handshake on reuse
        assert fresh_module_pool.size() == 1

    def test_two_different_sessions_each_spawn(self, monkeypatch, fresh_module_pool):
        rec = _SpawnAndTurnRecorder(
            session_ids=["00000000-0000-0000-0000-0000000000c3", "00000000-0000-0000-0000-0000000000c4"]
        )
        ch = ClaudeChannel()
        rec.patch(monkeypatch, ch)

        ch.run_turn("00000000-0000-0000-0000-0000000000c0", "ta", mode="fork")
        ch.run_turn("00000000-0000-0000-0000-0000000000c0", "tb", mode="fork")
        assert len(rec.spawn_calls) == 2
        assert fresh_module_pool.size() == 2
        assert set(fresh_module_pool.session_ids()) == {
            "00000000-0000-0000-0000-0000000000c3",
            "00000000-0000-0000-0000-0000000000c4",
        }

    def test_lru_eviction_at_pool_cap(self, monkeypatch, fresh_module_pool):
        # Force a small cap by replacing the pool entirely. _FakeClock makes
        # each turn's register() last_used strictly increasing (T-M2: no real
        # sleeps), so sA is unambiguously the evicted LRU entry.
        small_pool = ClaudePool(max_size=2, clock=_FakeClock())
        monkeypatch.setattr(pool_mod, "_pool", small_pool)

        rec = _SpawnAndTurnRecorder(
            session_ids=[
                "00000000-0000-0000-0000-0000000000c3",
                "00000000-0000-0000-0000-0000000000c4",
                "00000000-0000-0000-0000-0000000000c5",
            ]
        )
        ch = ClaudeChannel()
        rec.patch(monkeypatch, ch)

        ch.run_turn("00000000-0000-0000-0000-0000000000c1", "ta", mode="fork")
        ch.run_turn("00000000-0000-0000-0000-0000000000c1", "tb", mode="fork")
        ch.run_turn("00000000-0000-0000-0000-0000000000c1", "tc", mode="fork")

        # sA (oldest) should have been evicted.
        assert small_pool.size() == 2
        sids = set(small_pool.session_ids())
        assert sids == {"00000000-0000-0000-0000-0000000000c4", "00000000-0000-0000-0000-0000000000c5"}

    def test_timeout_in_pooled_turn_evicts(self, monkeypatch, fresh_module_pool):
        # First turn: succeed and pool under "00000000-0000-0000-0000-0000000000c6".
        rec = _SpawnAndTurnRecorder(session_ids=["00000000-0000-0000-0000-0000000000c6"])
        ch = ClaudeChannel()
        rec.patch(monkeypatch, ch)
        ch.run_turn("00000000-0000-0000-0000-0000000000c0", "ta", mode="fork")
        assert fresh_module_pool.size() == 1

        # Second turn: resume on "00000000-0000-0000-0000-0000000000c6", but _run_one_turn raises TurnTimeout.
        def boom(self_, entry, prompt, timeout, on_session_id, *, do_handshake, preallocated_session_id=None):
            raise TurnTimeout("simulated", partial="")

        monkeypatch.setattr(ClaudeChannel, "_run_one_turn", boom)

        with pytest.raises(TurnTimeout):
            ch.run_turn("00000000-0000-0000-0000-0000000000c6", "tb", mode="resume")

        # Failed pooled turn evicts the process.
        assert fresh_module_pool.size() == 0
        assert fresh_module_pool.acquire("00000000-0000-0000-0000-0000000000c6") is None

    def test_resume_cache_miss_spawns_and_pools(self, monkeypatch, fresh_module_pool):
        rec = _SpawnAndTurnRecorder(session_ids=["00000000-0000-0000-0000-0000000000c7"])
        ch = ClaudeChannel()
        rec.patch(monkeypatch, ch)

        # First call is mode=resume for a session never seen → spawn fresh.
        r = ch.run_turn("00000000-0000-0000-0000-0000000000c7", "go", mode="resume")
        assert r.session_id == "00000000-0000-0000-0000-0000000000c7"
        assert len(rec.spawn_calls) == 1
        # The recorder fed "00000000-0000-0000-0000-0000000000c7" back as the session id; pool now holds it.
        assert fresh_module_pool.session_ids() == ["00000000-0000-0000-0000-0000000000c7"]
        # Handshake was performed on the cache-miss spawn.
        assert rec.turn_calls[0][2] is True


class TestPoolModuleHelpers:
    """The module-level pool is lazily created on first use (so tests can swap
    it) and registers an atexit teardown exactly once. max_size and evict-of-
    a-missing-id are the small accessor/no-op branches the integration tests
    don't reach."""

    def test_max_size_exposes_cap(self):
        assert ClaudePool(max_size=5).max_size == 5

    def test_evict_missing_id_is_noop(self):
        ClaudePool(max_size=2).evict("never-registered")  # must not raise

    def test_get_pool_lazily_creates_and_registers_atexit(self, monkeypatch):
        registered: list = []
        monkeypatch.setattr(pool_mod.atexit, "register", registered.append)
        monkeypatch.setattr(pool_mod, "_pool", None)
        pool = _get_pool()
        assert isinstance(pool, ClaudePool)
        assert _get_pool() is pool  # cached, not re-created
        assert registered, "atexit teardown must be registered on creation"

    def test_shutdown_pool_tears_down_existing(self, monkeypatch):
        calls: list = []

        class _FakePool:
            def shutdown(self):
                calls.append(1)

        monkeypatch.setattr(pool_mod, "_pool", _FakePool())
        shutdown_pool()
        assert calls == [1]

    def test_shutdown_pool_noop_when_no_pool(self, monkeypatch):
        monkeypatch.setattr(pool_mod, "_pool", None)
        shutdown_pool()  # must not raise
