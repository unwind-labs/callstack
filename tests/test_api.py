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
    projects = tmp_path / "projects" / "fake-project"
    projects.mkdir(parents=True, exist_ok=True)

    class _Caller(Caller):
        def _invoke(self, tasks):
            parent = SessionRef(session_id="parent-id", file=parent_file)
            driver = self._scripted_driver()
            tree = driver.run(parent, tasks, base_depth=0)
            from agent_callstack import _results_from_tree  # type: ignore
            return _results_from_tree(tree)

        def _scripted_driver(self):
            return Driver(
                channel=channel,
                locator=SessionLocator(projects_dir=tmp_path / "projects"),
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
    p = tmp_path / "projects" / "fake-project"
    p.mkdir(parents=True, exist_ok=True)
    f = p / f"{name}.jsonl"
    f.write_text("")
    return f


# ---------- Single-task path ----------

class TestCall:

    def test_returns_result(self, tmp_path, parent_file):
        _make_clone(tmp_path, "child")
        ch = ScriptedChannel().respond(_envelope("return", result="hi", summary="s"), "child")
        caller = _make_caller(tmp_path, parent_file, ch)

        r = caller.call("do thing")
        assert isinstance(r, Result)
        assert r.value == "hi"
        assert r.summary == "s"

    def test_yield_raises(self, tmp_path, parent_file):
        clone = _make_clone(tmp_path, "yld")
        ch = ScriptedChannel().respond(_envelope("yield", question="MFA?"), "yld")
        caller = _make_caller(tmp_path, parent_file, ch)

        with pytest.raises(CallYielded) as excinfo:
            caller.call("auth")
        token = excinfo.value.token
        assert excinfo.value.question == "MFA?"
        assert token.session_id == "yld"
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
        _make_clone(tmp_path, "alpha")
        _make_clone(tmp_path, "bravo")
        responses = {
            "alpha": _envelope("return", result="A!"),
            "bravo": _envelope("yield", question="?"),
        }
        def respond(_src, prompt, _fork):
            tail = prompt.rsplit("\n\n", 1)[-1]
            for key, body in responses.items():
                if key in tail:
                    return TurnResult(
                        text=body, session_id=key, duration=0.0,
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
        clone = _make_clone(tmp_path, "child")
        ch = ScriptedChannel().respond(_envelope("yield", question="MFA?"), "child")
        caller = _make_caller(tmp_path, parent_file, ch)

        with pytest.raises(CallYielded) as info:
            caller.call("auth")
        token = info.value.token

        # Now respond to resume
        ch.respond(_envelope("return", result="ok"), "child")
        r = caller.resume(token, "847291")
        assert r.value == "ok"
