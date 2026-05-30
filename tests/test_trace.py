"""Tests for TraceWriter (append-only JSONL). TreeStore + _json_default moved to
test_tree_store.py alongside the module split."""

from __future__ import annotations

import json

from agent_callstack.trace import TraceWriter


def _base_kwargs(**overrides):
    """Minimal required kwargs for TraceWriter.write; tests override what they need."""
    base = dict(
        depth=1,
        task="t",
        session_id="s",
        result="",
        duration=0.0,
        api_request_id="req_test",
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        started_at_utc="2026-04-16T00:00:00+00:00",
        ended_at_utc="2026-04-16T00:00:01+00:00",
    )
    base.update(overrides)
    return base


class TestTraceWriter:
    def test_appends_jsonl(self, tmp_path):
        writer = TraceWriter(tmp_path / "traces")
        writer.write(
            **_base_kwargs(
                depth=1,
                task="t1",
                session_id="s1",
                result="hello",
                duration=1.23,
                api_request_id="req_abc",
                input_tokens=200,
                output_tokens=75,
                cache_read_tokens=150,
                cache_creation_tokens=50,
            )
        )
        writer.write(**_base_kwargs(depth=2, task="t2", session_id="s2", result="x", duration=0.5, error="boom"))

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
            "input_tokens": 200,
            "output_tokens": 75,
            "cache_read_tokens": 150,
            "cache_creation_tokens": 50,
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

    def test_concurrent_writes_do_not_interleave(self, tmp_path):
        """R-M3: the driver's worker threads share one TraceWriter and append
        concurrently. A single f.write() is not one syscall once the JSON line
        exceeds PIPE_BUF, so without the writer's lock concurrent appends can
        interleave and produce corrupt (unparseable) lines. Drive many threads
        writing large entries and assert every line parses and all are present."""
        import threading

        writer = TraceWriter(tmp_path / "traces")
        n_threads, per_thread = 8, 25
        # Large result -> long JSON line (> PIPE_BUF) to make interleaving
        # observable if the lock were removed.
        big = "x" * 20000
        start = threading.Event()

        def hammer(tid: int) -> None:
            start.wait()
            for k in range(per_thread):
                writer.write(**_base_kwargs(depth=tid, task=f"t{tid}-{k}", session_id=f"s{tid}", result=big))

        threads = [threading.Thread(target=hammer, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        start.set()  # release all threads at once to maximize contention
        for t in threads:
            t.join()

        lines = (tmp_path / "traces" / "call_trace.jsonl").read_text().splitlines()
        assert len(lines) == n_threads * per_thread
        # Every line must be independently parseable — no torn/interleaved JSON.
        for line in lines:
            json.loads(line)
