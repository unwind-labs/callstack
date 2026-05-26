"""Tests for the public API: call / call_many / resume / Caller.

These tests inject a ScriptedChannel via Caller's `_driver_for` override so the
end-to-end translation from Tree → Result/CallYielded/CallFailed is verified
without spawning subprocesses."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_callstack import (
    Caller, CallFailed, CallYielded, MultiResult, Result, YieldToken,
)
from agent_callstack.channel import ScriptedChannel
from agent_callstack.driver import Driver
from agent_callstack.session import SessionLocator, SessionRef
from agent_callstack.trace import TraceWriter, TreeStore


def _envelope(op: str, **fields) -> str:
    return "```json\n" + json.dumps({"op": op, **fields}) + "\n```"


@pytest.fixture
def parent_file(tmp_path):
    f = tmp_path / "parent.jsonl"
    f.write_text(json.dumps({"cwd": str(tmp_path), "type": "user"}) + "\n")
    return f


def _make_caller(tmp_path, parent_file, channel: ScriptedChannel) -> Caller:
    """Subclass Caller so all driver creation uses the scripted channel."""
    from agent_callstack.session import encode_project_dir
    projects = tmp_path / "projects" / encode_project_dir(str(tmp_path))
    projects.mkdir(parents=True, exist_ok=True)

    class _Caller(Caller):
        def _invoke(self, tasks, *, context: str = "fork"):
            parent = SessionRef(session_id="00000000-0000-0000-0000-0000000000d2", file=parent_file)
            driver = self._scripted_driver()
            tree = driver.run(parent, tasks, base_depth=0, context=context)
            from agent_callstack import _results_from_tree  # type: ignore
            return _results_from_tree(tree)

        def _scripted_driver(self):
            return Driver(
                channel=channel,
                resolve_session=SessionLocator(projects_dir=tmp_path / "projects").resolve,
                trace=TraceWriter(tmp_path / "traces"),
                store=TreeStore(),
                cwd=str(tmp_path),
                timeout=10,
                max_depth=5,
            )

        def resume(self, token, reply):
            from agent_callstack.driver import Tree
            store = TreeStore()
            snapshot = store.load(Path(token.clone_path))
            assert snapshot is not None
            tree = Tree.from_dict(snapshot)
            driver = self._scripted_driver()
            driver.resume(tree, token.session_id, reply)
            from agent_callstack import _results_from_tree, _unwrap_single  # type: ignore
            return _unwrap_single(_results_from_tree(tree)[0])

    return _Caller()


def _make_clone(tmp_path, name) -> Path:
    from agent_callstack.session import encode_project_dir
    p = tmp_path / "projects" / encode_project_dir(str(tmp_path))
    p.mkdir(parents=True, exist_ok=True)
    f = p / f"{name}.jsonl"
    f.write_text("")
    return f


# ---------- Single-task path ----------

class TestCall:

    def test_returns_result(self, tmp_path, parent_file):
        _make_clone(tmp_path, "00000000-0000-0000-0000-0000000000d5")
        ch = ScriptedChannel().respond(_envelope("return", result="hi", summary="s"), "00000000-0000-0000-0000-0000000000d5")
        caller = _make_caller(tmp_path, parent_file, ch)

        r = caller.call("do thing")
        assert isinstance(r, Result)
        assert r.value == "hi"
        assert r.summary == "s"

    def test_yield_raises(self, tmp_path, parent_file):
        clone = _make_clone(tmp_path, "00000000-0000-0000-0000-0000000000d0")
        ch = ScriptedChannel().respond(_envelope("yield", question="MFA?"), "00000000-0000-0000-0000-0000000000d0")
        caller = _make_caller(tmp_path, parent_file, ch)

        with pytest.raises(CallYielded) as excinfo:
            caller.call("auth")
        token = excinfo.value.token
        assert excinfo.value.question == "MFA?"
        assert token.session_id == "00000000-0000-0000-0000-0000000000d0"
        assert Path(token.clone_path) == clone

    def test_failure_raises(self, tmp_path, parent_file):
        def boom(*_):
            raise RuntimeError("bad CLI")
        ch = ScriptedChannel().respond_with(boom)
        caller = _make_caller(tmp_path, parent_file, ch)

        with pytest.raises(CallFailed):
            caller.call("doomed")


# ---------- call_many ----------

class TestCallMany:

    def test_mixed_results(self, tmp_path, parent_file):
        from agent_callstack.channel import TurnResult
        _make_clone(tmp_path, "00000000-0000-0000-0000-0000000000d6")
        _make_clone(tmp_path, "00000000-0000-0000-0000-0000000000d7")
        responses = {
            "alpha": ("00000000-0000-0000-0000-0000000000d6",
                      _envelope("return", result="A!")),
            "bravo": ("00000000-0000-0000-0000-0000000000d7",
                      _envelope("yield", question="?")),
        }
        def respond(_src, prompt, _fork):
            tail = prompt.rsplit("\n\n", 1)[-1]
            for tag, (sid, body) in responses.items():
                if tag in tail:
                    return TurnResult(
                        text=body, session_id=sid, duration=0.0,
                        api_request_id="", input_tokens=0, output_tokens=0,
                        cache_read_tokens=0, cache_creation_tokens=0,
                        total_cost_usd=0.0,
                    )
            raise AssertionError(prompt[:80])
        ch = ScriptedChannel().respond_with(respond).respond_with(respond)
        caller = _make_caller(tmp_path, parent_file, ch)

        out = caller.call_many(["task alpha", "task bravo"])
        assert isinstance(out, MultiResult)
        assert len(out.results) == 2
        kinds = {type(r).__name__ for r in out.results}
        assert kinds == {"Result", "CallYielded"}


# ---------- Resume ----------

class TestResume:

    def test_resume_completes(self, tmp_path, parent_file):
        clone = _make_clone(tmp_path, "00000000-0000-0000-0000-0000000000d5")
        ch = ScriptedChannel().respond(_envelope("yield", question="MFA?"), "00000000-0000-0000-0000-0000000000d5")
        caller = _make_caller(tmp_path, parent_file, ch)

        with pytest.raises(CallYielded) as info:
            caller.call("auth")
        token = info.value.token

        # Now respond to resume
        ch.respond(_envelope("return", result="ok"), "00000000-0000-0000-0000-0000000000d5")
        r = caller.resume(token, "847291")
        assert r.value == "ok"


class TestResumeRealMethod:
    """Exercises the REAL ``Caller.resume`` (snapshot load → context resolve →
    report/reporter/seal wiring → unwrap), not the test-double override used in
    ``TestResume``. This is the public path a host runs when answering a
    CallYielded, so its happy and fail-closed branches must both be pinned."""

    _SID = "00000000-0000-0000-0000-0000000000d8"

    def _scripted_driver(self, tmp_path, channel: ScriptedChannel) -> Driver:
        return Driver(
            channel=channel,
            resolve_session=SessionLocator(projects_dir=tmp_path / "projects").resolve,
            trace=TraceWriter(tmp_path / "traces"),
            store=TreeStore(),
            cwd=str(tmp_path),
            timeout=10,
            max_depth=5,
        )

    def _setup_yield(self, tmp_path, parent_file) -> YieldToken:
        """Drive a real yield so the on-disk state (saved snapshot at a
        resolvable clone path) matches what a host holds when it gets a token."""
        clone = _make_clone(tmp_path, self._SID)
        parent = SessionRef(
            session_id="00000000-0000-0000-0000-0000000000d2", file=parent_file)
        ch = ScriptedChannel().respond(
            _envelope("yield", question="MFA?"), self._SID)
        tree = self._scripted_driver(tmp_path, ch).run(parent, ["auth"])
        leaf = tree.yielded_leaves()[0]
        assert Path(leaf.clone_path or "") == clone
        return YieldToken(session_id=leaf.session_id, clone_path=leaf.clone_path or "")

    def test_real_resume_completes(self, tmp_path, parent_file):
        """Resuming a yielded call must load the persisted tree, drive the
        resume turn through the channel, seal a report, and return the child's
        Result — the end-to-end contract resume() promises a host."""
        token = self._setup_yield(tmp_path, parent_file)
        resume_ch = ScriptedChannel().respond(
            _envelope("return", result="done"), self._SID)

        test = self

        class _Caller(Caller):
            def _driver_for(self, parent, *, ctx, depth_base=0):
                return test._scripted_driver(tmp_path, resume_ch)

        caller = _Caller(log_dir=tmp_path / "logs",
                         invoke_id="20260101T000000-deadbeef")
        r = caller.resume(token, "847291")
        assert isinstance(r, Result)
        assert r.value == "done"
        # A report.yaml must have been sealed under the resume's invocation dir.
        assert (tmp_path / "logs" / "20260101T000000-deadbeef").exists()

    def test_real_resume_missing_snapshot_fails(self, tmp_path):
        """A token whose clone path has no .call_tree sidecar must raise
        CallFailed (fail loud), never fabricate an empty Result. Real Caller,
        no channel override — it must bail before spawning anything."""
        caller = Caller(log_dir=tmp_path / "logs",
                        invoke_id="20260101T000000-deadbeef")
        token = YieldToken(session_id="x", clone_path=str(tmp_path / "ghost.jsonl"))
        with pytest.raises(CallFailed, match="cannot resume"):
            caller.resume(token, "reply")


def test_module_level_resume_delegates(monkeypatch):
    """The module-level ``resume()`` wrapper must delegate to a Caller's
    ``resume`` (via the shared/override resolver), so callers get the same
    behavior whether they use the function or the object."""
    import agent_callstack as ac

    captured = {}

    class _Stub:
        def resume(self, token, reply):
            captured["args"] = (token, reply)
            return "delegated"

    monkeypatch.setattr(ac, "_resolve_caller", lambda seed, timeout: _Stub())
    token = YieldToken(session_id="s", clone_path="/tmp/x.jsonl")
    assert ac.resume(token, "the-reply") == "delegated"
    assert captured["args"] == (token, "the-reply")


class TestEnvPropagation:
    """The Caller stamps env onto every spawned `claude` subprocess via
    the ClaudeChannel constructed in ``_driver_for``. Grandchildren read
    these vars to discover their depth and budget."""

    def test_max_depth_is_stamped_onto_child_env(self, tmp_path):
        """CORR-101: a Caller built with ``max_depth=N`` must stamp
        ``CALLSTACK_MAX_DEPTH=N`` onto the spawn env.

        Without this, a grandchild's Driver falls back to the default cap
        (10) — silently exceeding a budget the root explicitly chose."""
        from agent_callstack.session import SessionRef
        caller = Caller(max_depth=3)
        parent = SessionRef(
            session_id="00000000-0000-0000-0000-0000000000aa",
            file=tmp_path / "p.jsonl",
        )
        ctx = caller._resolve_invocation_context(parent)
        driver = caller._driver_for(parent, ctx=ctx)

        env = driver.channel._env_extra
        assert env.get("CALLSTACK_MAX_DEPTH") == "3", (
            f"expected CALLSTACK_MAX_DEPTH=3 on spawn env, got "
            f"{env.get('CALLSTACK_MAX_DEPTH')!r}; without this, a "
            f"grandchild reverts to the default cap and exceeds the "
            f"budget the root chose"
        )

    def test_default_max_depth_also_stamped(self, tmp_path):
        """Even when the user doesn't set max_depth explicitly, the env
        carries the effective value — so the cap is uniform across the
        whole subtree rather than dependent on whether each child happens
        to inherit ENV_MAX_DEPTH from its shell."""
        from agent_callstack.session import SessionRef
        caller = Caller()  # max_depth defaults to 10
        parent = SessionRef(
            session_id="00000000-0000-0000-0000-0000000000bb",
            file=tmp_path / "p.jsonl",
        )
        ctx = caller._resolve_invocation_context(parent)
        driver = caller._driver_for(parent, ctx=ctx)
        assert driver.channel._env_extra.get("CALLSTACK_MAX_DEPTH") == "10"
