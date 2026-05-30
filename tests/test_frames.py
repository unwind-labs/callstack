"""Unit tests for frames.py — the on-disk frame loader, orphan reconciliation,
graft helpers, and tree-walk / log-line formatters.

These target the pure-function branches the end-to-end driver/reporter tests
don't reach: malformed-input tolerance in `_load_frames`, the liveness/age
edges of orphan reconciliation, and the status-rollup table. Each test pins a
behavior the merged report depends on (a frame that won't parse must be
skipped, not crash the reporter tick), per Rule 9 — not just line coverage.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import agent_callstack.frames as fr
import agent_callstack.state as st
import pytest
from agent_callstack.driver import Node, Tree
from agent_callstack.session import SessionRef


@pytest.fixture(autouse=True)
def _clear_frame_caches():
    """Frame caches are module-global; clear before and after each test so
    one test's parsed/dir snapshots can't leak into the next."""
    fr._frames_cache_clear()
    yield
    fr._frames_cache_clear()


# ---------- _pid_alive ----------


class TestPidAlive:
    def test_own_pid_is_alive(self):
        assert fr._pid_alive(os.getpid()) is True

    def test_invalid_pid_is_dead(self):
        assert fr._pid_alive(0) is False
        assert fr._pid_alive(-5) is False
        assert fr._pid_alive("nope") is False  # type: ignore[arg-type]

    def test_permission_error_means_alive(self, monkeypatch):
        """EPERM from kill(pid, 0) means the process EXISTS but we can't
        signal it — that's 'alive', the safe direction for reconciliation."""

        def raise_perm(_pid, _sig):
            raise PermissionError()

        monkeypatch.setattr(fr.os, "kill", raise_perm)
        assert fr._pid_alive(99999) is True

    def test_generic_oserror_means_dead(self, monkeypatch):
        def raise_os(_pid, _sig):
            raise OSError("boom")

        monkeypatch.setattr(fr.os, "kill", raise_os)
        assert fr._pid_alive(99999) is False

    def test_process_lookup_means_dead(self, monkeypatch):
        def raise_lookup(_pid, _sig):
            raise ProcessLookupError()

        monkeypatch.setattr(fr.os, "kill", raise_lookup)
        assert fr._pid_alive(99999) is False


# ---------- _frame_age_seconds ----------


class TestFrameAgeSeconds:
    def test_missing_started_at_is_unknown(self):
        assert fr._frame_age_seconds({}) is None

    def test_non_string_started_at_is_unknown(self):
        assert fr._frame_age_seconds({"started_at": 12345}) is None

    def test_empty_started_at_is_unknown(self):
        assert fr._frame_age_seconds({"started_at": ""}) is None

    def test_unparseable_started_at_is_unknown(self):
        assert fr._frame_age_seconds({"started_at": "not-a-date"}) is None

    def test_z_suffix_parsed_as_utc(self):
        # 10s after the frame's stamp.
        now = dt.datetime(2026, 1, 1, 0, 0, 10, tzinfo=dt.timezone.utc).timestamp()
        age = fr._frame_age_seconds({"started_at": "2026-01-01T00:00:00Z"}, now=now)
        assert age == pytest.approx(10.0, abs=0.01)

    def test_naive_timestamp_treated_as_utc(self):
        """A timestamp with no tzinfo (externally-produced frame) must be
        treated as UTC, not crash — line 134."""
        now = dt.datetime(2026, 1, 1, 0, 1, 0, tzinfo=dt.timezone.utc).timestamp()
        age = fr._frame_age_seconds({"started_at": "2026-01-01T00:00:00"}, now=now)
        assert age == pytest.approx(60.0, abs=0.01)


# ---------- _frame_writer_is_dead ----------


class TestFrameWriterIsDead:
    def test_no_writer_pid_is_not_dead(self):
        assert fr._frame_writer_is_dead({}, ttl_seconds=10) is False

    def test_non_int_writer_pid_is_not_dead(self):
        assert fr._frame_writer_is_dead({"writer_pid": "x"}, ttl_seconds=10) is False

    def test_dead_pid_is_dead(self, monkeypatch):
        monkeypatch.setattr(fr, "_pid_alive", lambda _pid: False)
        assert fr._frame_writer_is_dead({"writer_pid": 1234}, ttl_seconds=10) is True

    def test_alive_pid_zero_ttl_opts_out(self, monkeypatch):
        """ttl_seconds=0 disables the age fallback — liveness alone."""
        monkeypatch.setattr(fr, "_pid_alive", lambda _pid: True)
        assert fr._frame_writer_is_dead({"writer_pid": 1234}, ttl_seconds=0) is False

    def test_alive_pid_unknown_age_is_not_dead(self, monkeypatch):
        monkeypatch.setattr(fr, "_pid_alive", lambda _pid: True)
        # No started_at → age unknown → not declared dead.
        assert fr._frame_writer_is_dead({"writer_pid": 1234}, ttl_seconds=10) is False

    def test_alive_pid_past_ttl_is_dead(self, monkeypatch):
        """PID reuse defense: an alive-looking pid whose frame is older than
        the TTL is treated as a reclaimed pid → dead."""
        monkeypatch.setattr(fr, "_pid_alive", lambda _pid: True)
        now = dt.datetime(2026, 1, 1, 1, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        frame = {"writer_pid": 1234, "started_at": "2026-01-01T00:00:00Z"}
        assert fr._frame_writer_is_dead(frame, ttl_seconds=60, now=now) is True


# ---------- mark_abandoned_in_dict_nodes ----------


class TestMarkAbandoned:
    def test_non_dict_node_skipped(self):
        nodes = ["garbage", 42, None]
        assert fr.mark_abandoned_in_dict_nodes(nodes, reason="r") == 0

    def test_node_without_dict_state_skipped_but_children_walked(self):
        """A node whose `state` isn't a dict is skipped, but its children
        list must still be recursed (branch 210->222)."""
        nodes = [
            {
                "state": "not-a-dict",
                "children": [{"state": {"kind": "awaiting_turn"}, "session_id": "s1"}],
            }
        ]
        changed = fr.mark_abandoned_in_dict_nodes(nodes, reason="dead")
        assert changed == 1
        assert nodes[0]["children"][0]["state"]["kind"] == "abandoned"

    def test_non_list_children_tolerated(self):
        """`children` that isn't a list must not crash the walk (223->206)."""
        nodes = [{"state": {"kind": "awaiting_turn"}, "children": "oops"}]
        changed = fr.mark_abandoned_in_dict_nodes(nodes, reason="dead")
        assert changed == 1

    def test_eligible_node_rewritten_with_error_and_session(self):
        nodes = [{"state": {"kind": "awaiting_child", "session_id": "sess-9"}}]
        changed = fr.mark_abandoned_in_dict_nodes(nodes, reason="writer pid 7 gone")
        assert changed == 1
        new_state = nodes[0]["state"]
        assert new_state["kind"] == "abandoned"
        assert new_state["session_id"] == "sess-9"
        assert "writer pid 7 gone" in new_state["error"]
        assert "writer pid 7 gone" in nodes[0]["error"]

    def test_terminal_node_not_touched(self):
        nodes = [{"state": {"kind": "done"}}]
        assert fr.mark_abandoned_in_dict_nodes(nodes, reason="r") == 0


# ---------- _reconcile_orphan_states ----------


class TestReconcileOrphanStates:
    def test_live_writer_left_alone(self, monkeypatch):
        monkeypatch.setattr(fr, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(fr, "read_orphan_ttl_seconds", lambda: 0)
        frames = {"k": [{"writer_pid": 1, "tree": {"nodes": [{"state": {"kind": "awaiting_turn"}}]}}]}
        fr._reconcile_orphan_states(frames)
        assert frames["k"][0]["tree"]["nodes"][0]["state"]["kind"] == "awaiting_turn"

    def test_dead_writer_promotes_nodes(self, monkeypatch):
        monkeypatch.setattr(fr, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(fr, "read_orphan_ttl_seconds", lambda: 0)
        frames = {"k": [{"writer_pid": 9, "tree": {"nodes": [{"state": {"kind": "awaiting_turn"}}]}}]}
        fr._reconcile_orphan_states(frames)
        assert frames["k"][0]["tree"]["nodes"][0]["state"]["kind"] == "abandoned"

    def test_dead_writer_non_dict_tree_skipped(self, monkeypatch):
        """A dead writer whose `tree` isn't a dict must be skipped, not
        crash (line 184)."""
        monkeypatch.setattr(fr, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(fr, "read_orphan_ttl_seconds", lambda: 0)
        frames = {"k": [{"writer_pid": 9, "tree": "not-a-dict"}]}
        fr._reconcile_orphan_states(frames)  # must not raise

    def test_dead_writer_non_list_nodes_skipped(self, monkeypatch):
        """tree.nodes that isn't a list must be skipped (line 187)."""
        monkeypatch.setattr(fr, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(fr, "read_orphan_ttl_seconds", lambda: 0)
        frames = {"k": [{"writer_pid": 9, "tree": {"nodes": "oops"}}]}
        fr._reconcile_orphan_states(frames)  # must not raise


# ---------- _load_frames ----------


class TestLoadFrames:
    def test_missing_dir_returns_empty(self, tmp_path):
        assert fr._load_frames(tmp_path / "does-not-exist") == {}

    def test_loads_and_groups_by_key(self, tmp_path):
        d = tmp_path / "_frames"
        d.mkdir()
        (d / "a.yaml").write_text("frame_key: root\nstarted_at: '2026-01-01T00:00:00Z'\ntree: {nodes: []}\n")
        (d / "b.yaml").write_text("frame_key: abc123\nstarted_at: '2026-01-01T00:00:01Z'\ntree: {nodes: []}\n")
        out = fr._load_frames(d)
        assert set(out.keys()) == {"root", "abc123"}

    def test_malformed_yaml_skipped(self, tmp_path, capsys):
        """A frame that won't parse must be skipped and logged, not crash
        the reporter tick (lines 305-314)."""
        d = tmp_path / "_frames"
        d.mkdir()
        (d / "bad.yaml").write_text("{[ this is not valid yaml")
        (d / "good.yaml").write_text("frame_key: root\ntree: {nodes: []}\n")
        out = fr._load_frames(d)
        assert set(out.keys()) == {"root"}
        assert "ignoring malformed frame file" in capsys.readouterr().err

    def test_non_dict_yaml_skipped(self, tmp_path):
        """YAML that parses to a non-dict (e.g. a list) must be skipped
        (line 316)."""
        d = tmp_path / "_frames"
        d.mkdir()
        (d / "list.yaml").write_text("- just\n- a\n- list\n")
        (d / "good.yaml").write_text("frame_key: root\ntree: {nodes: []}\n")
        assert set(fr._load_frames(d).keys()) == {"root"}

    def test_bad_frame_key_rejected(self, tmp_path):
        """A frame_key that doesn't match the SEC-006 shape is dropped
        (line 327)."""
        d = tmp_path / "_frames"
        d.mkdir()
        (d / "x.yaml").write_text("frame_key: 'has spaces and / slashes'\ntree: {nodes: []}\n")
        assert fr._load_frames(d) == {}

    def test_missing_frame_key_rejected(self, tmp_path):
        d = tmp_path / "_frames"
        d.mkdir()
        (d / "x.yaml").write_text("tree: {nodes: []}\n")
        assert fr._load_frames(d) == {}

    def test_oversized_file_skipped(self, tmp_path, capsys, monkeypatch):
        """Files over the byte cap are skipped with a warning (lines 286-290)."""
        monkeypatch.setattr(fr, "_MAX_FRAME_FILE_BYTES", 10)
        d = tmp_path / "_frames"
        d.mkdir()
        (d / "big.yaml").write_text("frame_key: root\ntree: {nodes: []}\n" * 5)
        assert fr._load_frames(d) == {}
        assert "skipping oversized frame file" in capsys.readouterr().err

    def test_too_many_files_breaks_with_warning(self, tmp_path, capsys, monkeypatch):
        """Beyond _MAX_FRAMES_PER_LOAD the scan breaks (lines 275-280)."""
        monkeypatch.setattr(fr, "_MAX_FRAMES_PER_LOAD", 2)
        d = tmp_path / "_frames"
        d.mkdir()
        for i in range(5):
            (d / f"f{i}.yaml").write_text(f"frame_key: k{i}\ntree: {{nodes: []}}\n")
        out = fr._load_frames(d)
        assert len(out) == 2
        assert "further frames ignored" in capsys.readouterr().err

    def test_per_file_stat_oserror_skips_file(self, tmp_path, monkeypatch):
        """A file whose stat() raises (e.g. removed mid-scan) must be skipped,
        not crash the load (lines 283-284)."""
        d = tmp_path / "_frames"
        d.mkdir()
        doomed = d / "vanishing.yaml"
        doomed.write_text("frame_key: gone\ntree: {nodes: []}\n")
        (d / "good.yaml").write_text("frame_key: root\ntree: {nodes: []}\n")

        real_stat = os.stat

        def flaky_stat(path, *args, **kwargs):
            if str(path) == str(doomed):
                raise OSError("file vanished mid-scan")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(fr.os, "stat", flaky_stat)
        out = fr._load_frames(d)
        assert set(out.keys()) == {"root"}

    def test_dir_mtime_fast_path_returns_cached(self, tmp_path, monkeypatch):
        """A second load with unchanged dir mtime reuses the cached snapshot
        (deep-copied) instead of re-globbing."""
        d = tmp_path / "_frames"
        d.mkdir()
        (d / "a.yaml").write_text("frame_key: root\ntree: {nodes: []}\n")
        first = fr._load_frames(d)
        # Make a subsequent glob blow up; if the fast-path works it's never called.
        monkeypatch.setattr(type(d), "glob", lambda *a, **k: (_ for _ in ()).throw(AssertionError("glob called")))
        second = fr._load_frames(d)
        assert second == first

    def test_dir_stat_oserror_disables_cache(self, tmp_path, monkeypatch):
        """If the dir stat raises, dir_mtime is None and the cache is
        bypassed (lines 256-257, branches 258->272 and 336->343) — the load
        still succeeds via the slow path."""
        d = tmp_path / "_frames"
        d.mkdir()
        (d / "a.yaml").write_text("frame_key: root\ntree: {nodes: []}\n")

        real_stat = os.stat
        dir_stat_calls = {"n": 0}

        def flaky_stat(path, *args, **kwargs):
            # `is_dir()` stats the dir first (must succeed so we reach the
            # dir-mtime stat); the explicit `frames_dir.stat()` at line 255 is
            # the second stat of the dir — that's the one we make fail.
            if str(path) == str(d):
                dir_stat_calls["n"] += 1
                if dir_stat_calls["n"] == 2:
                    raise OSError("stat denied")
            return real_stat(path, *args, **kwargs)

        # Path.stat() funnels through os.stat.
        monkeypatch.setattr(fr.os, "stat", flaky_stat)
        out = fr._load_frames(d)
        assert set(out.keys()) == {"root"}


# ---------- cache eviction ----------


class TestCacheEviction:
    def test_dir_cache_evicts_oldest(self, monkeypatch):
        monkeypatch.setattr(fr, "_FRAMES_DIR_CACHE_MAX", 2)
        from pathlib import Path

        fr._cache_put_dir(Path("/a"), (1, {}))
        fr._cache_put_dir(Path("/b"), (1, {}))
        fr._cache_put_dir(Path("/c"), (1, {}))  # evicts /a (line 90)
        assert Path("/a") not in fr._FRAMES_DIR_CACHE
        assert set(fr._FRAMES_DIR_CACHE.keys()) == {Path("/b"), Path("/c")}

    def test_parsed_cache_evicts_oldest(self, monkeypatch):
        monkeypatch.setattr(fr, "_FRAMES_PARSED_CACHE_MAX", 2)
        from pathlib import Path

        fr._cache_put_parsed(Path("/a"), (1, 1, {}))
        fr._cache_put_parsed(Path("/b"), (1, 1, {}))
        fr._cache_put_parsed(Path("/c"), (1, 1, {}))
        assert Path("/a") not in fr._FRAMES_PARSED_CACHE


# ---------- _status_of_nodes ----------


class TestStatusOfNodes:
    def test_empty(self):
        assert fr._status_of_nodes([]) == "empty"

    def test_all_complete(self):
        assert fr._status_of_nodes([{"status": "complete"}, {"status": "complete"}]) == "complete"

    def test_yielded_wins(self):
        assert fr._status_of_nodes([{"status": "complete"}, {"status": "yielded"}]) == "yielded"

    def test_all_error(self):
        assert fr._status_of_nodes([{"status": "error"}]) == "error"

    def test_all_timeout(self):
        assert fr._status_of_nodes([{"status": "timeout"}]) == "timeout"

    def test_all_abandoned(self):
        assert fr._status_of_nodes([{"status": "abandoned"}]) == "abandoned"

    def test_mixed(self):
        assert fr._status_of_nodes([{"status": "complete"}, {"status": "error"}]) == "mixed"


# ---------- graft helpers ----------


class TestGraftHelpers:
    def test_grafted_children_matches_by_id(self):
        node = {"id": "abc", "children": [{"id": "kid"}]}
        nested = {"abc": [{"tree": {"nodes": [{"id": "grafted"}]}}]}
        kids = fr._grafted_children(node, nested)
        ids = {k["id"] for k in kids}
        assert ids == {"kid", "grafted"}

    def test_grafted_children_falls_back_to_session_id(self):
        node = {"id": "abc", "session_id": "sess-1", "children": []}
        nested = {"sess-1": [{"tree": {"nodes": [{"id": "via-session"}]}}]}
        kids = fr._grafted_children(node, nested)
        assert [k["id"] for k in kids] == ["via-session"]

    def test_merge_raw_nodes_no_root_returns_empty(self):
        """_merge_raw_nodes with no root frame returns [] (line 497)."""
        assert fr._merge_raw_nodes({"abc": [{"tree": {"nodes": []}}]}) == []

    def test_merge_raw_nodes_grafts(self):
        frames = {
            "root": [{"tree": {"nodes": [{"id": "r", "session_id": "s"}]}}],
            "r": [{"tree": {"nodes": [{"id": "child"}]}}],
        }
        out = fr._merge_raw_nodes(frames)
        assert out[0]["id"] == "r"
        assert out[0]["children"][0]["id"] == "child"

    def test_build_merged_report_rolls_up(self):
        root_frame = {
            "tree": {"nodes": [{"id": "n1", "state": {"kind": "done"}, "duration": 1.0}], "base_depth": 0},
            "kind": "fork",
            "cwd": "/x",
            "started_at": "2026-01-01T00:00:00Z",
            "tasks": ["the task"],
        }
        report = fr._build_merged_report(
            invoke_id="inv-1", frames={"root": [root_frame]}, root_frame=root_frame, ended_at="2026-01-01T00:01:00Z"
        )
        assert report["invoke_id"] == "inv-1"
        assert report["status"] == "complete"
        assert report["tasks"][0]["input"] == "the task"


# ---------- _chain_to_session ----------


class TestChainToSession:
    def test_finds_by_full_id(self):
        nodes = [{"id": "aaaaaaaa1111", "children": [{"id": "bbbbbbbb2222", "session_id": "sx"}]}]
        chain = fr._chain_to_session(nodes, "bbbbbbbb2222")
        assert chain == ["aaaaaaaa", "bbbbbbbb"]

    def test_finds_by_session_id(self):
        nodes = [{"id": "aaaaaaaa1111", "session_id": "sess-top", "children": []}]
        assert fr._chain_to_session(nodes, "sess-top") == ["aaaaaaaa"]

    def test_not_found_returns_none(self):
        assert fr._chain_to_session([{"id": "x", "children": []}], "missing") is None

    def test_non_dict_node_skipped(self):
        """A non-dict entry in the node list must be skipped, not crash
        (line 517)."""
        nodes = ["garbage", {"id": "realid00", "session_id": "s", "children": []}]
        assert fr._chain_to_session(nodes, "s") == ["realid00"]


# ---------- _walk_tree / _format_log_line ----------


def _tree_with(nodes: list[Node]) -> Tree:
    return Tree(
        root_session=SessionRef(session_id="00000000-0000-0000-0000-0000000000aa", file=Path("/tmp/x")),
        nodes=nodes,
        base_depth=0,
    )


class TestWalkTree:
    def test_walks_children_with_chain(self):
        child = Node(id="b" * 32, task="child", state=st.Done(result="ok"))
        root = Node(id="a" * 32, task="root", state=st.Done(result="ok"), children=[child])
        tree = _tree_with([root])
        walked = list(fr._walk_tree(tree))
        # root then child; child's chain includes root's short id (line 470).
        assert walked[0][0].task == "root"
        assert walked[1][0].task == "child"
        assert walked[1][2] == ["a" * 8]


class TestFormatLogLine:
    def _node(self, **kw):
        return Node(id="a" * 32, task="t", state=kw.pop("state", st.Done(result="r")), **kw)

    def test_complete_shows_result(self):
        n = self._node(state=st.Done(result="the answer"))
        line = fr._format_log_line("12:00", n, 1, chain=[])
        assert 'result="the answer"' in line

    def test_error_shows_error(self):
        """The error branch (line 484) renders the error detail."""
        n = self._node(state=st.Failed(error="it broke"))
        line = fr._format_log_line("12:00", n, 1, chain=[])
        assert 'error="it broke"' in line

    def test_yielded_shows_awaiting(self):
        n = self._node(state=st.AwaitingUser(question="q?", session_id="s"))
        line = fr._format_log_line("12:00", n, 1, chain=[])
        assert "(awaiting user)" in line


# ---------- _one_line sanitization ----------


class TestOneLine:
    def test_strips_control_chars(self):
        # ESC (0x1b) and a bell (0x07) become '?'; newline/tab collapse to space.
        out = fr._one_line("a\x1bb\x07c\nd\te", 100)
        assert out == "a?b?c d e"

    def test_double_quote_becomes_single(self):
        assert fr._one_line('say "hi"', 100) == "say 'hi'"

    def test_truncates_with_ellipsis(self):
        out = fr._one_line("x" * 100, 10)
        assert out.endswith("…")
        assert len(out) == 10
