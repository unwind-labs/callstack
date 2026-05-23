"""Tests for fix #2 (atomic child-returned → node terminal at MCP boundary)
and fix #3 (shutdown / signal hardening) of the stuck-running-child bug.

Together with `test_orphan_reconciliation.py` (fix #1) these pin the
three layers that prevent unwind from rendering a permanent in-progress
spinner on a CALL row whose child already returned to its parent.

Fix #2 (`reporter._finalize_own_frames`) is the synchronous belt-and-
suspenders the MCP server runs before emitting `tool_result` to the
parent agent. It promotes any non-terminal node in a frame *this
process* owns to the synthetic terminal kind ``Abandoned``.

Fix #3 (the active-reporter registry + chained atexit / signal handlers)
is the catch-all for the case where the MCP boundary is never reached
because the process is being torn down — SIGTERM, SIGINT, an unhandled
exception escaping the asyncio loop, or a normal `sys.exit`. Each live
`_LiveReporter` writes a post-mortem frame from its in-memory tree so
the merged report still settles.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml

import agent_callstack as ac
from agent_callstack import _InvocationContext, _ROOT_FRAME_KEY
from agent_callstack import state as st
from agent_callstack import reporter as reporter_mod
from agent_callstack import shutdown as shutdown_mod
from agent_callstack.driver import Node, Tree
from agent_callstack.reporter import (
    _LiveReporter,
    _abandon_frame_nodes_in_place,
    _abandon_tree_nodes_in_place,
    _atomic_yaml_write,
    _finalize_own_frames,
    _flush_active_reporters,
    _register_active_reporter,
    _unregister_active_reporter,
)
from agent_callstack.session import SessionRef


# ---------- shared helpers ----------

def _running_node(nid: str, task: str = "t", *,
                  session_id: str | None = None) -> Node:
    sid = session_id or f"sess-{nid}"
    return Node(
        id=nid, task=task,
        state=st.AwaitingTurn(session_id=sid),
        session_id=sid,
    )


def _ctx(tmp_path: Path, *, frame_key: str = _ROOT_FRAME_KEY,
         is_nested: bool = False) -> _InvocationContext:
    return _InvocationContext(
        invoke_id="inv-shutdown-test",
        log_dir=tmp_path / "log",
        cwd=str(tmp_path),
        frame_key=frame_key,
        is_nested=is_nested,
    )


def _make_reporter(tmp_path: Path, **ctx_kwargs) -> _LiveReporter:
    ctx = _ctx(tmp_path, **ctx_kwargs)
    ctx.invocation_dir.mkdir(parents=True, exist_ok=True)
    ctx.frames_dir.mkdir(parents=True, exist_ok=True)
    return _LiveReporter(ctx=ctx, kind="call", tasks=["t"], started_at="s")


def _write_frame_with(tmp_path: Path, *, frame_key: str, tree: Tree,
                      writer_pid: int) -> Path:
    """Write a frame YAML file in the shape the reporter would produce
    and return its path so tests can probe / re-read it."""
    log_dir = tmp_path / "log"
    invocation_dir = log_dir / "inv-shutdown-test"
    frames_dir = invocation_dir / "_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    frame = {
        "frame_key": frame_key,
        "is_nested": frame_key != _ROOT_FRAME_KEY,
        "kind": "call", "tasks": ["t"], "cwd": str(tmp_path),
        "writer_pid": writer_pid,
        "started_at": "s", "ended_at": "e",
        "tree": tree.to_dict(),
    }
    path = frames_dir / f"{frame_key}.yaml"
    _atomic_yaml_write(path, frame)
    return path


def _drain_registered_reporters() -> None:
    """Test isolation: drop any reporters that earlier tests created but
    never finalize()d (typically because the test crafted a reporter
    instance just to probe behavior). Without this, `_flush_active_reporters`
    in later tests would walk reporters owned by *other* tests."""
    with reporter_mod._ACTIVE_REPORTERS_LOCK:
        reporter_mod._ACTIVE_REPORTERS.clear()


@pytest.fixture(autouse=True)
def _clear_reporter_registry():
    """Every test starts with an empty active-reporter registry — the
    fixture order matters because some tests assert exact registry
    membership and stale entries from prior tests would corrupt that."""
    _drain_registered_reporters()
    ac._frames_cache_clear()
    yield
    _drain_registered_reporters()
    ac._frames_cache_clear()


# ==========================================================
# Fix #2: atomic finalize at MCP boundary
# ==========================================================

class TestAbandonTreeInPlace:
    """`_abandon_tree_nodes_in_place` is the in-memory primitive both
    the shutdown handler and tests use to convert a Tree's non-terminal
    nodes to the synthetic `Abandoned` terminal kind."""

    def test_promotes_running_root_to_abandoned(self):
        parent = SessionRef(session_id="p", file=Path("/tmp/p.jsonl"))
        node = _running_node("aaaaaaaa")
        tree = Tree(root_session=parent, nodes=[node], base_depth=0)

        n = _abandon_tree_nodes_in_place(tree, reason="testing")

        assert n == 1
        assert isinstance(node.state, st.Abandoned)
        assert "testing" in node.state.error
        assert "awaiting_turn" in node.state.error, (
            "the error message must surface the prior kind so post-mortem "
            "consumers can see *what* the node was doing when sealed"
        )
        assert node.error == node.state.error

    def test_recurses_into_children(self):
        parent = SessionRef(session_id="p", file=Path("/tmp/p.jsonl"))
        grandchild = _running_node("gggggggg")
        child = _running_node("cccccccc")
        child.children = [grandchild]
        root = _running_node("rrrrrrrr")
        root.children = [child]
        tree = Tree(root_session=parent, nodes=[root], base_depth=0)

        n = _abandon_tree_nodes_in_place(tree, reason="r")

        assert n == 3
        assert isinstance(root.state, st.Abandoned)
        assert isinstance(child.state, st.Abandoned)
        assert isinstance(grandchild.state, st.Abandoned)

    def test_leaves_terminal_nodes_alone(self):
        """A completed node must not be clobbered by the abandonment
        pass — that would erase real results."""
        parent = SessionRef(session_id="p", file=Path("/tmp/p.jsonl"))
        done = Node(
            id="dddddddd", task="t",
            state=st.Done(session_id="s", result="kept"),
            session_id="s", result="kept",
        )
        tree = Tree(root_session=parent, nodes=[done], base_depth=0)

        n = _abandon_tree_nodes_in_place(tree, reason="r")

        assert n == 0
        assert isinstance(done.state, st.Done)
        assert done.result == "kept"

    def test_leaves_awaiting_user_alone(self):
        """AwaitingUser is parked legitimately waiting for a user
        reply — promoting it to Abandoned would lose the yield intent
        and the parent would never get a chance to resume()."""
        parent = SessionRef(session_id="p", file=Path("/tmp/p.jsonl"))
        yielded = Node(
            id="yyyyyyyy", task="t",
            state=st.AwaitingUser(session_id="s", question="who?"),
            session_id="s",
        )
        tree = Tree(root_session=parent, nodes=[yielded], base_depth=0)

        n = _abandon_tree_nodes_in_place(tree, reason="r")

        assert n == 0
        assert isinstance(yielded.state, st.AwaitingUser)


class TestAbandonFrameInPlace:
    """`_abandon_frame_nodes_in_place` mirrors the in-memory variant but
    operates on raw `Node.to_dict()` payloads loaded from frame YAML.
    Used by `_finalize_own_frames` to avoid a tree-reconstruction round
    trip on the MCP boundary path."""

    def test_promotes_running_dict_to_abandoned_kind(self):
        nodes = [{
            "id": "x", "task": "t",
            "state": {"kind": "awaiting_turn", "session_id": "s"},
            "children": [],
        }]
        n = _abandon_frame_nodes_in_place(nodes, reason="boundary")
        assert n == 1
        assert nodes[0]["state"]["kind"] == "abandoned"
        assert "boundary" in nodes[0]["state"]["error"]
        assert nodes[0]["state"]["session_id"] == "s", (
            "session_id must be preserved so chain-to-session lookups "
            "still resolve after abandonment"
        )
        assert "boundary" in nodes[0]["error"]

    def test_does_not_clobber_existing_error(self):
        nodes = [{
            "id": "x", "task": "t", "error": "pre-existing",
            "state": {"kind": "awaiting_child"},
            "children": [],
        }]
        _abandon_frame_nodes_in_place(nodes, reason="r")
        assert nodes[0]["error"] == "pre-existing", (
            "if the node already carried an error message, the abandonment "
            "pass must not overwrite it — that message is closer to the "
            "true cause"
        )

    def test_skips_awaiting_user_kind(self):
        nodes = [{
            "id": "x", "task": "t",
            "state": {"kind": "awaiting_user", "session_id": "s",
                      "question": "?"},
            "children": [],
        }]
        n = _abandon_frame_nodes_in_place(nodes, reason="r")
        assert n == 0
        assert nodes[0]["state"]["kind"] == "awaiting_user"

    def test_recurses_into_children(self):
        nodes = [{
            "id": "p", "task": "p",
            "state": {"kind": "awaiting_child"},
            "children": [{
                "id": "c", "task": "c",
                "state": {"kind": "awaiting_turn"},
                "children": [],
            }],
        }]
        n = _abandon_frame_nodes_in_place(nodes, reason="r")
        assert n == 2


class TestFinalizeOwnFrames:
    """`_finalize_own_frames` is the synchronous guard the MCP server
    runs before emitting `tool_result`. It scans the invocation's
    `_frames/` directory, finds frames written by *this process*, and
    promotes any non-terminal nodes to Abandoned."""

    def test_no_op_when_invocation_dir_missing(self, tmp_path):
        # No frames_dir exists yet — must return False cleanly so the
        # MCP boundary doesn't error on a never-started invocation.
        rewrote = _finalize_own_frames(
            tmp_path / "log", "missing-inv", reason="r",
        )
        assert rewrote is False

    def test_rewrites_own_pid_frame_with_non_terminal_nodes(self, tmp_path):
        parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
        node = _running_node("aaaa1111")
        tree = Tree(root_session=parent, nodes=[node], base_depth=0)
        frame_path = _write_frame_with(
            tmp_path, frame_key=_ROOT_FRAME_KEY, tree=tree,
            writer_pid=os.getpid(),
        )

        rewrote = _finalize_own_frames(
            tmp_path / "log", "inv-shutdown-test",
            reason="boundary",
        )

        assert rewrote is True
        reloaded = yaml.safe_load(frame_path.read_text())
        [n] = reloaded["tree"]["nodes"]
        assert n["state"]["kind"] == "abandoned"
        assert "boundary" in n["state"]["error"]

    def test_skips_frames_owned_by_other_pid(self, tmp_path):
        """Critical invariant for nested MCP: a child invocation's
        boundary must NOT finalize the parent's root frame, which lives
        in the same `_frames/` dir but is still being driven by the
        parent process."""
        parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
        node = _running_node("bbbb2222")
        tree = Tree(root_session=parent, nodes=[node], base_depth=0)
        # Pretend the frame was written by a different live PID. Use
        # PID 1 (init) — it's always alive and never us.
        other_pid = 1 if os.getpid() != 1 else 2
        frame_path = _write_frame_with(
            tmp_path, frame_key=_ROOT_FRAME_KEY, tree=tree,
            writer_pid=other_pid,
        )

        rewrote = _finalize_own_frames(
            tmp_path / "log", "inv-shutdown-test", reason="r",
        )

        assert rewrote is False
        reloaded = yaml.safe_load(frame_path.read_text())
        [n] = reloaded["tree"]["nodes"]
        assert n["state"]["kind"] == "awaiting_turn", (
            "frames owned by other processes must be left untouched — "
            "otherwise nested-MCP boundary firings would clobber the "
            "still-running parent invocation's root frame"
        )

    def test_idempotent_on_already_terminal_frame(self, tmp_path):
        """Calling twice must produce the same result; second call must
        not double-rewrite (no on-disk churn) and must return False."""
        parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
        node = _running_node("cccc3333")
        tree = Tree(root_session=parent, nodes=[node], base_depth=0)
        _write_frame_with(
            tmp_path, frame_key=_ROOT_FRAME_KEY, tree=tree,
            writer_pid=os.getpid(),
        )

        first = _finalize_own_frames(
            tmp_path / "log", "inv-shutdown-test", reason="r",
        )
        second = _finalize_own_frames(
            tmp_path / "log", "inv-shutdown-test", reason="r",
        )

        assert first is True
        assert second is False, (
            "second pass must be a no-op — the frame's nodes are all "
            "terminal now and there's nothing left to promote"
        )

    def test_returns_false_when_only_terminal_nodes(self, tmp_path):
        parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
        done = Node(
            id="ffff4444", task="t",
            state=st.Done(session_id="s", result="ok"),
            session_id="s", result="ok",
        )
        tree = Tree(root_session=parent, nodes=[done], base_depth=0)
        _write_frame_with(
            tmp_path, frame_key=_ROOT_FRAME_KEY, tree=tree,
            writer_pid=os.getpid(),
        )

        rewrote = _finalize_own_frames(
            tmp_path / "log", "inv-shutdown-test", reason="r",
        )
        assert rewrote is False


class TestInvokeFallsBackToCurrentTree:
    """If `Driver.run` raises before returning, `_invoke`'s try/finally
    must still call `reporter.finalize` — otherwise the frame stays in
    whatever state the last `on_progress` tick recorded, and the bug
    we're fixing (#2) recurs. Fall back to `driver.last_tree`,
    which `Driver.run` stamps onto itself BEFORE doing any work that
    might raise."""

    def test_falls_back_when_driver_run_raises_after_tree_stamp(
        self, tmp_path, monkeypatch,
    ):
        from agent_callstack import _LiveReporter
        from agent_callstack.driver import Driver
        from agent_callstack.session import SessionLocator

        # Zero the late-envelope wait budget; otherwise the fallback
        # tree's partial AwaitingTurn node would burn the full 120s
        # default budget polling for a JSONL that doesn't exist.
        monkeypatch.setenv("CALLSTACK_FINALIZE_WAIT_SECONDS", "0")

        captured_trees: list[Tree] = []
        real_finalize = _LiveReporter.finalize

        def capture(self, tree):
            captured_trees.append(tree)
            return real_finalize(self, tree)
        monkeypatch.setattr(_LiveReporter, "finalize", capture)

        # Patch Driver.run to stamp a partial tree then raise — the
        # exact sequence the bug describes (driver dies mid-run).
        parent_file = tmp_path / "p.jsonl"
        parent_file.write_text("")
        parent = SessionRef(session_id="p", file=parent_file)
        monkeypatch.setattr(
            SessionLocator, "locate",
            lambda self, **kwargs: parent,
        )
        # Also patch SessionLocator() construction call site in _invoke;
        # locate() is the only method it uses.

        def explosive_run(self, parent, tasks, base_depth=0, context="fork"):
            node = self._new_node(tasks[0])
            self.last_tree = Tree(
                root_session=parent, nodes=[node], base_depth=base_depth,
            )
            raise RuntimeError("simulated driver crash")
        monkeypatch.setattr(Driver, "run", explosive_run)

        caller = ac.Caller(
            cwd=str(tmp_path),
            log_dir=tmp_path / "log",
            invoke_id="inv-fallback-test",
        )
        with pytest.raises(RuntimeError, match="simulated driver crash"):
            caller.call_many(["t"])

        assert captured_trees, (
            "reporter.finalize must have been invoked even though "
            "Driver.run raised — without the fallback the frame would "
            "never be sealed and unwind would spin forever"
        )
        [t] = captured_trees
        # The fallback tree is the partial one Driver stamped onto itself.
        assert len(t.nodes) == 1
        assert t.nodes[0].task == "t"


# ==========================================================
# Fix #3: shutdown / signal hardening
# ==========================================================

class TestActiveReporterRegistry:
    def test_construction_registers_reporter(self, tmp_path):
        with reporter_mod._ACTIVE_REPORTERS_LOCK:
            before = len(reporter_mod._ACTIVE_REPORTERS)
        r = _make_reporter(tmp_path)
        with reporter_mod._ACTIVE_REPORTERS_LOCK:
            after = len(reporter_mod._ACTIVE_REPORTERS)
            assert r in reporter_mod._ACTIVE_REPORTERS
        assert after == before + 1

    def test_finalize_unregisters_reporter(self, tmp_path):
        r = _make_reporter(tmp_path)
        parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
        done = Node(
            id="ddddeeee", task="t",
            state=st.Done(session_id="s", result="ok"),
            session_id="s", result="ok",
        )
        tree = Tree(root_session=parent, nodes=[done], base_depth=0)
        r.finalize(tree)
        with reporter_mod._ACTIVE_REPORTERS_LOCK:
            assert r not in reporter_mod._ACTIVE_REPORTERS, (
                "finalize() must drop the reporter from the shutdown "
                "registry — leaving it would cause the atexit handler to "
                "re-write a frame that's already authoritatively sealed"
            )

    def test_register_unregister_are_idempotent(self, tmp_path):
        r = _make_reporter(tmp_path)
        _register_active_reporter(r)
        _register_active_reporter(r)  # duplicate
        with reporter_mod._ACTIVE_REPORTERS_LOCK:
            count = sum(1 for x in reporter_mod._ACTIVE_REPORTERS if x is r)
        assert count == 1
        _unregister_active_reporter(r)
        _unregister_active_reporter(r)  # extra discard
        with reporter_mod._ACTIVE_REPORTERS_LOCK:
            assert r not in reporter_mod._ACTIVE_REPORTERS


class TestEmergencyFinalizeOnShutdown:
    """`_LiveReporter._emergency_finalize_on_shutdown` is what the
    atexit / signal handler invokes on every active reporter. It must:

      * Promote non-terminal nodes in the in-memory tree to Abandoned.
      * Write a frame so the post-mortem report surfaces the change.
      * Skip cleanly when finalize already ran or no tree was observed.
      * Never raise — the caller is in a shutdown path."""

    def test_writes_abandoned_frame_for_unfinalized_reporter(self, tmp_path):
        r = _make_reporter(tmp_path)
        parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
        node = _running_node("aaaa9999")
        tree = Tree(root_session=parent, nodes=[node], base_depth=0)
        # Simulate the reporter having seen at least one tick.
        r(tree)

        r._emergency_finalize_on_shutdown()

        # The in-memory tree is mutated in place.
        assert isinstance(node.state, st.Abandoned)
        # And the on-disk frame reflects it.
        frame_path = r._ctx.frame_path()
        reloaded = yaml.safe_load(frame_path.read_text())
        [n] = reloaded["tree"]["nodes"]
        assert n["state"]["kind"] == "abandoned"

    def test_no_op_when_already_finalized(self, tmp_path):
        r = _make_reporter(tmp_path)
        parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
        done = Node(
            id="bbbb8888", task="t",
            state=st.Done(session_id="s", result="real"),
            session_id="s", result="real",
        )
        tree = Tree(root_session=parent, nodes=[done], base_depth=0)
        r.finalize(tree)  # this sets _finalized + unregisters

        # Re-add for the test so we can call _emergency_finalize on it
        # — production wouldn't normally do this, but it asserts that
        # the guard inside _emergency_finalize_on_shutdown holds.
        _register_active_reporter(r)
        # State the precondition: nothing should be mutated.
        r._emergency_finalize_on_shutdown()

        assert isinstance(done.state, st.Done), (
            "_emergency_finalize_on_shutdown must skip when _finalized is "
            "True — otherwise it would clobber the real terminal state "
            "that the normal finalize() already wrote"
        )

    def test_no_op_when_no_tree_observed(self, tmp_path):
        # Construct a reporter but never call it; _latest_tree stays None.
        r = _make_reporter(tmp_path)
        # Must not raise.
        r._emergency_finalize_on_shutdown()
        # And no frame should have been written.
        assert not r._ctx.frame_path().exists()

    def test_never_raises_on_write_failure(self, tmp_path, monkeypatch):
        """Shutdown is the worst time to surface an exception — every
        sibling reporter still needs its shot at finalization."""
        r = _make_reporter(tmp_path)
        parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
        node = _running_node("cccc7777")
        tree = Tree(root_session=parent, nodes=[node], base_depth=0)
        r(tree)

        def boom(*a, **kw):
            raise OSError("disk gone")
        # Force the frame write to fail; the routine must swallow it.
        monkeypatch.setattr(r, "_write_frame", boom)

        r._emergency_finalize_on_shutdown()  # must not raise


class TestFlushActiveReporters:
    """`_flush_active_reporters` is what atexit / SIGTERM / SIGINT
    actually invoke. It must walk every registered reporter and run
    `_emergency_finalize_on_shutdown` on each, surviving any individual
    failure."""

    def test_walks_every_reporter(self, tmp_path):
        # Two separate invocation contexts so they don't share frames_dir.
        r1 = _make_reporter(tmp_path / "a")
        r2 = _make_reporter(tmp_path / "b")
        parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
        for r in (r1, r2):
            node = _running_node(f"node-{id(r)}"[:8])
            tree = Tree(root_session=parent, nodes=[node], base_depth=0)
            r(tree)

        _flush_active_reporters()

        for r in (r1, r2):
            frame_path = r._ctx.frame_path()
            assert frame_path.exists()
            reloaded = yaml.safe_load(frame_path.read_text())
            [n] = reloaded["tree"]["nodes"]
            assert n["state"]["kind"] == "abandoned"

    def test_one_reporter_failing_does_not_block_siblings(
        self, tmp_path, monkeypatch,
    ):
        good = _make_reporter(tmp_path / "good")
        bad = _make_reporter(tmp_path / "bad")
        parent = SessionRef(session_id="p", file=tmp_path / "p.jsonl")
        for r in (good, bad):
            node = _running_node(f"node-{id(r)}"[:8])
            tree = Tree(root_session=parent, nodes=[node], base_depth=0)
            r(tree)

        # Make `bad`'s emergency-finalize blow up. `good` must still run.
        def explode(*a, **kw):
            raise RuntimeError("intentional")
        monkeypatch.setattr(
            bad, "_emergency_finalize_on_shutdown", explode,
        )

        _flush_active_reporters()  # must not raise

        good_frame = yaml.safe_load(good._ctx.frame_path().read_text())
        [n] = good_frame["tree"]["nodes"]
        assert n["state"]["kind"] == "abandoned", (
            "a single misbehaving reporter must not block its siblings "
            "from finalizing — otherwise one rogue invocation could "
            "leave the whole report broken"
        )


class TestSignalHandlerChain:
    """Signal handler installation must preserve prior dispositions and
    chain to them on fire, so we never break an operator's
    KeyboardInterrupt or a parent's SIGTERM handling."""

    def test_chain_signal_handler_runs_flush_then_calls_prev(
        self, tmp_path, monkeypatch,
    ):
        # Don't actually deliver a signal in the test — install the
        # handler explicitly and invoke its target callable.
        calls: list[str] = []

        def prev(signum, frame):
            calls.append(f"prev({signum})")

        monkeypatch.setattr(signal, "getsignal", lambda sig: prev)
        installed: dict = {}
        monkeypatch.setattr(
            signal, "signal",
            lambda sig, handler: installed.setdefault("handler", handler),
        )

        # Also stub the flush so we don't need real reporters. The
        # canonical implementation lives in `shutdown`; the reporter
        # re-export is just an alias, so we patch the source.
        monkeypatch.setattr(
            shutdown_mod, "flush_active_reporters",
            lambda: calls.append("flushed"),
        )

        shutdown_mod._chain_signal_handler(signal.SIGTERM)
        handler = installed["handler"]
        handler(signal.SIGTERM, None)

        assert calls == ["flushed", f"prev({int(signal.SIGTERM)})"], (
            "the chained handler must flush reporters FIRST (so we get "
            "post-mortem frames) then delegate to whatever was installed "
            "before us, preserving prior signal semantics"
        )

    def test_chain_signal_handler_tolerates_unsupported_signal(
        self, monkeypatch,
    ):
        # Some sandboxes don't allow signal installation. Must not raise.
        def fake_signal(sig, handler):
            raise ValueError("not allowed")
        monkeypatch.setattr(signal, "signal", fake_signal)
        # Must complete without propagating.
        shutdown_mod._chain_signal_handler(signal.SIGTERM)


class TestShutdownInstallIsIdempotent:
    """REVIEW-202: shutdown hooks are installed exactly once per process
    by `install_shutdown_hooks()`, called at module-load time on the main
    thread. Reporters register/unregister themselves but never trigger
    the install — so an MCP server constructing reporters from worker
    threads still gets correct signal hooks (the previous design silently
    skipped the install when the constructor ran off the main thread)."""

    def test_install_hooks_is_idempotent(self, monkeypatch):
        """Calling `install_shutdown_hooks` N times only runs atexit
        registration once."""
        atexit_calls: list = []
        signal_calls: list = []
        monkeypatch.setattr(
            shutdown_mod.atexit, "register",
            lambda fn: atexit_calls.append(fn),
        )
        monkeypatch.setattr(
            shutdown_mod.signal, "signal",
            lambda sig, handler: signal_calls.append((sig, handler)),
        )
        shutdown_mod._reset_for_tests()
        try:
            shutdown_mod.install_shutdown_hooks()
            shutdown_mod.install_shutdown_hooks()
            shutdown_mod.install_shutdown_hooks()
            # Snapshot the count BEFORE the restore step so the restore's
            # own install doesn't pollute the assertion.
            installs_under_test = len(atexit_calls)
        finally:
            # Restore the live state so subsequent tests don't see a
            # "never installed" flag.
            shutdown_mod._reset_for_tests()
            shutdown_mod.install_shutdown_hooks()
        assert installs_under_test == 1, (
            "install_shutdown_hooks must register atexit exactly once, "
            "regardless of how many times it's called"
        )

    def test_registering_reporters_does_not_reinstall(self, tmp_path,
                                                      monkeypatch):
        """Reporter construction must NOT trigger signal install. The
        previous architecture installed-on-first-register and silently
        skipped when that "first register" came from a worker thread."""
        atexit_calls: list = []
        signal_calls: list = []
        monkeypatch.setattr(
            shutdown_mod.atexit, "register",
            lambda fn: atexit_calls.append(fn),
        )
        monkeypatch.setattr(
            shutdown_mod.signal, "signal",
            lambda sig, handler: signal_calls.append((sig, handler)),
        )
        # Constructing reporters with hooks ALREADY installed must not
        # re-register anything.
        r1 = _make_reporter(tmp_path / "a")
        r2 = _make_reporter(tmp_path / "b")
        r3 = _make_reporter(tmp_path / "c")
        assert atexit_calls == [], (
            "constructing reporters must not re-register atexit hooks"
        )
        assert signal_calls == [], (
            "constructing reporters must not re-install signal handlers"
        )
        # Reporters DID land in the registry though.
        with shutdown_mod._ACTIVE_REPORTERS_LOCK:
            assert r1 in shutdown_mod._ACTIVE_REPORTERS
            assert r2 in shutdown_mod._ACTIVE_REPORTERS
            assert r3 in shutdown_mod._ACTIVE_REPORTERS

    def test_install_from_worker_thread_skips_signal_handlers(
        self, monkeypatch,
    ):
        """REVIEW-202 regression: `signal.signal()` only runs on the main
        thread. The new contract is that `install_shutdown_hooks()` from
        a worker thread skips signal install (returns False) but still
        registers atexit (which has no thread restriction). Previously
        the constructor-side install ran from worker threads and silently
        dropped signal handlers on the floor."""
        atexit_calls: list = []
        signal_calls: list = []
        monkeypatch.setattr(
            shutdown_mod.atexit, "register",
            lambda fn: atexit_calls.append(fn),
        )
        monkeypatch.setattr(
            shutdown_mod.signal, "signal",
            lambda sig, handler: signal_calls.append((sig, handler)),
        )
        shutdown_mod._reset_for_tests()
        result_holder: dict = {}

        def install_from_worker():
            result_holder["installed_signals"] = (
                shutdown_mod.install_shutdown_hooks()
            )
        try:
            t = threading.Thread(target=install_from_worker)
            t.start()
            t.join()
            installs_under_test = len(atexit_calls)
            signal_under_test = list(signal_calls)
        finally:
            shutdown_mod._reset_for_tests()
            shutdown_mod.install_shutdown_hooks()
        assert result_holder["installed_signals"] is False, (
            "install_shutdown_hooks called from a worker thread must "
            "return False (no signal handlers wired)"
        )
        # Worker-thread install still wires atexit — that has no
        # main-thread restriction in CPython.
        assert installs_under_test == 1
        assert signal_under_test == [], (
            "signal.signal must NOT be invoked from a worker thread — "
            "the previous code did this silently, breaking SIGTERM/SIGINT"
        )


# ==========================================================
# End-to-end: a process that gets SIGTERM'd mid-run leaves a
# report.yaml that settles to status="abandoned" rather than
# spinning forever.
# ==========================================================

class TestEndToEndSigtermProducesAbandonedReport:
    """Spawn a real Python subprocess that constructs a reporter with a
    running node, then SIGTERM it. The atexit handler should flush, the
    frame should be Abandoned, and the merged report should settle.

    This is the closest test we can write to the actual bug repro: a
    parent driver killed mid-flight. If this passes, the fix #3
    signal-chaining is doing its job under real process-shutdown
    conditions, not just in-process simulation."""

    def test_sigterm_produces_abandoned_frame(self, tmp_path):
        # Locate the repo's import roots so the child process can find
        # `agent_callstack`.
        repo = Path(__file__).resolve().parents[1]
        plugin_root = repo / "plugins" / "callstack"

        runner = tmp_path / "child.py"
        runner.write_text(f"""
import os
import sys
import time
sys.path.insert(0, {str(plugin_root)!r})
from pathlib import Path
import agent_callstack as ac
from agent_callstack import _InvocationContext, _ROOT_FRAME_KEY
from agent_callstack import state as st
from agent_callstack.driver import Node, Tree
from agent_callstack.reporter import _LiveReporter
from agent_callstack.session import SessionRef

ctx = _InvocationContext(
    invoke_id='sigterm-test',
    log_dir=Path({str(tmp_path)!r}) / 'log',
    cwd={str(tmp_path)!r},
    frame_key=_ROOT_FRAME_KEY,
    is_nested=False,
)
ctx.invocation_dir.mkdir(parents=True, exist_ok=True)
ctx.frames_dir.mkdir(parents=True, exist_ok=True)
r = _LiveReporter(ctx=ctx, kind='call', tasks=['t'], started_at='s')

parent = SessionRef(session_id='p', file=Path({str(tmp_path)!r}) / 'p.jsonl')
node = Node(
    id='a' * 32, task='t',
    state=st.AwaitingTurn(session_id='sess-x'),
    session_id='sess-x',
)
tree = Tree(root_session=parent, nodes=[node], base_depth=0)
r(tree)  # one tick so _latest_tree is set
print('READY', flush=True)
# Park forever; parent will SIGTERM us.
while True:
    time.sleep(0.5)
""")
        proc = subprocess.Popen(
            [sys.executable, str(runner)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # Wait for the child to announce readiness so we know the
            # reporter is live and the frame has at least one tick.
            assert proc.stdout is not None
            ready = proc.stdout.readline()
            assert ready.strip() == "READY", (
                f"child failed to start: stdout={ready!r}, "
                f"stderr={proc.stderr.read() if proc.stderr else ''!r}"
            )

            os.kill(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                pytest.fail("child did not respond to SIGTERM in 5s")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

        frame_path = (
            tmp_path / "log" / "sigterm-test" / "_frames"
            / f"{_ROOT_FRAME_KEY}.yaml"
        )
        assert frame_path.exists(), (
            f"signal handler must have flushed a frame to disk before "
            f"the child exited; nothing at {frame_path}. "
            f"stderr: {proc.stderr.read() if proc.stderr else ''!r}"
        )
        reloaded = yaml.safe_load(frame_path.read_text())
        [n] = reloaded["tree"]["nodes"]
        assert n["state"]["kind"] == "abandoned", (
            "post-SIGTERM frame must surface 'abandoned' so the merged "
            "report stops rendering an in-progress spinner — got "
            f"kind={n['state']['kind']!r}"
        )
