"""Tests for TraceWriter (append JSONL) and TreeStore (sidecar snapshots)."""
from __future__ import annotations

import json
from pathlib import Path

from agent_callstack.trace import TraceWriter, TreeStore


def _base_kwargs(**overrides):
    """Minimal required kwargs for TraceWriter.write; tests override what they need."""
    base = dict(
        depth=1, task="t", session_id="s", result="", duration=0.0,
        api_request_id="req_test", input_tokens=100, output_tokens=50,
        cache_read_tokens=0, cache_creation_tokens=0,
        started_at_utc="2026-04-16T00:00:00+00:00",
        ended_at_utc="2026-04-16T00:00:01+00:00",
    )
    base.update(overrides)
    return base


class TestTraceWriter:

    def test_appends_jsonl(self, tmp_path):
        writer = TraceWriter(tmp_path / "traces")
        writer.write(**_base_kwargs(depth=1, task="t1", session_id="s1",
                                    result="hello", duration=1.23,
                                    api_request_id="req_abc",
                                    input_tokens=200, output_tokens=75,
                                    cache_read_tokens=150,
                                    cache_creation_tokens=50))
        writer.write(**_base_kwargs(depth=2, task="t2", session_id="s2",
                                    result="x", duration=0.5, error="boom"))

        path = tmp_path / "traces" / "call_trace.jsonl"
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 2

        entry1 = json.loads(lines[0])
        assert entry1["call_depth"] == 1
        assert entry1["task"] == "t1"
        assert entry1["result_length"] == 5
        assert entry1["error"] is None
        assert entry1["api_request_id"] == "req_abc"
        assert entry1["usage"] == {
            "input_tokens": 200, "output_tokens": 75,
            "cache_read_tokens": 150, "cache_creation_tokens": 50,
        }
        assert entry1["timestamp"] == "2026-04-16T00:00:00+00:00"
        assert entry1["ended_at"] == "2026-04-16T00:00:01+00:00"
        assert entry1["seed"] is None

        entry2 = json.loads(lines[1])
        assert entry2["error"] == "boom"

    def test_truncates_long_task(self, tmp_path):
        writer = TraceWriter(tmp_path / "traces")
        writer.write(**_base_kwargs(task="x" * 500))
        entry = json.loads((tmp_path / "traces" / "call_trace.jsonl").read_text())
        assert len(entry["task"]) == 200

    def test_seed_recorded(self, tmp_path):
        writer = TraceWriter(tmp_path / "traces")
        writer.write(**_base_kwargs(seed=42))
        entry = json.loads((tmp_path / "traces" / "call_trace.jsonl").read_text())
        assert entry["seed"] == 42


class TestTreeStore:

    def test_save_and_load_round_trip(self, tmp_path):
        clone = tmp_path / "clone.jsonl"
        clone.write_text("")
        store = TreeStore()
        snapshot = {"hello": "world", "n": 42}
        store.save(clone, snapshot)
        sidecar = Path(str(clone) + ".call_tree")
        assert sidecar.exists()

        loaded = store.load(clone)
        assert loaded == snapshot
        # load is destructive
        assert not sidecar.exists()

    def test_load_missing_returns_none(self, tmp_path):
        store = TreeStore()
        assert store.load(tmp_path / "ghost") is None

    def test_concurrent_load_one_winner(self, tmp_path):
        """SEC-007: two threads racing to load the same sidecar must yield
        exactly one winner; the loser sees None, no exception escapes."""
        import threading
        clone = tmp_path / "clone.jsonl"
        clone.write_text("")
        store = TreeStore()
        snapshot = {"only": "once"}
        store.save(clone, snapshot)

        results: list = []
        errors: list = []
        start = threading.Event()

        def race():
            start.wait()
            try:
                results.append(store.load(clone))
            except Exception as e:  # pragma: no cover - guarded by assertion
                errors.append(e)

        threads = [threading.Thread(target=race) for _ in range(8)]
        # Spawn all 8 threads BEFORE releasing the barrier. Setting start
        # inside the spawn loop (the prior bug) let the first thread run to
        # completion before the rest existed, so the SEC-007 race this test
        # claims to exercise never actually happened.
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join()

        assert errors == [], f"load raised under race: {errors}"
        winners = [r for r in results if r is not None]
        losers = [r for r in results if r is None]
        assert len(winners) == 1, f"expected one winner, got {len(winners)}"
        assert winners[0] == snapshot
        assert len(losers) == len(threads) - 1
