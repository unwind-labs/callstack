"""Tests for TreeStore (execution-tree resume sidecars) and its _json_default
fallback. Split out of test_trace.py alongside the module split."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from agent_callstack.tree_store import TreeStore, _json_default


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


@dataclass
class _Snap:
    a: int
    b: str


class TestJsonDefault:
    """_json_default is the json.dump fallback for TreeStore snapshots. It must
    serialize the two non-JSON-native types the tree carries — dataclasses and
    Paths — and raise TypeError for anything else so a silently-dropped field
    can't corrupt a snapshot the resume path depends on."""

    def test_dataclass_instance_becomes_dict(self):
        assert _json_default(_Snap(a=1, b="x")) == {"a": 1, "b": "x"}

    def test_path_becomes_string(self):
        p = Path("/tmp/x")
        assert _json_default(p) == str(p)

    def test_dataclass_type_is_not_serialized(self):
        # The class object (not an instance) must NOT be asdict'd — it falls
        # through to the TypeError, guarding the `not isinstance(obj, type)`.
        with pytest.raises(TypeError):
            _json_default(_Snap)

    def test_unsupported_type_raises_typeerror(self):
        with pytest.raises(TypeError, match="not JSON serializable"):
            _json_default(object())

    def test_used_as_json_dump_default_for_snapshot(self, tmp_path):
        """End-to-end: a snapshot containing a dataclass + Path round-trips
        through TreeStore.save (which wires _json_default as the dump default)."""
        clone = tmp_path / "clone.jsonl"
        clone.write_text("")
        store = TreeStore()
        store.save(clone, {"snap": _Snap(a=2, b="y"), "where": tmp_path})
        loaded = store.load(clone)
        assert loaded == {"snap": {"a": 2, "b": "y"}, "where": str(tmp_path)}


def test_treestore_reexported_from_trace():
    """Back-compat: `from agent_callstack.trace import TreeStore` must keep
    resolving to the same class now that it lives in tree_store."""
    import agent_callstack.trace as trace
    import agent_callstack.tree_store as tree_store

    assert trace.TreeStore is tree_store.TreeStore
