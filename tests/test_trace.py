"""Tests for TraceWriter (append JSONL) and TreeStore (sidecar snapshots)."""
from __future__ import annotations

import json
from pathlib import Path

from agent_callstack.trace import TraceWriter, TreeStore


class TestTraceWriter:

    def test_appends_jsonl(self, tmp_path):
        writer = TraceWriter(tmp_path / "traces")
        writer.write(depth=1, task="t1", session_id="s1",
                     result="hello", duration=1.23)
        writer.write(depth=2, task="t2", session_id="s2",
                     result="x", duration=0.5, error="boom")

        path = tmp_path / "traces" / "call_trace.jsonl"
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 2

        entry1 = json.loads(lines[0])
        assert entry1["call_depth"] == 1
        assert entry1["task"] == "t1"
        assert entry1["result_length"] == 5
        assert entry1["error"] is None

        entry2 = json.loads(lines[1])
        assert entry2["error"] == "boom"

    def test_truncates_long_task(self, tmp_path):
        writer = TraceWriter(tmp_path / "traces")
        writer.write(depth=1, task="x" * 500, session_id="s",
                     result="", duration=0.0)
        entry = json.loads((tmp_path / "traces" / "call_trace.jsonl").read_text())
        assert len(entry["task"]) == 200


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
