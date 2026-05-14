"""Test-only Channel implementation.

`ScriptedChannel` returns canned text for each turn so the entire driver
and state machine can be exercised without spawning a real `claude`
subprocess. Kept out of `channel.py` so the production module's surface
stays small.

For backward compatibility, `ScriptedChannel` (plus the
`ScriptedResponse` / `ScriptedEntry` type aliases) is also re-exported
from `agent_callstack.channel`.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Optional, Union

from .channel import TurnResult, _fire_on_session_id


ScriptedResponse = Callable[[str, str, str], TurnResult]
ScriptedEntry = Union[tuple[str, str], ScriptedResponse]


@dataclass
class ScriptedChannel:
    """Test channel that returns scripted text for each turn.

    Each entry in `responses` is either a `(text, session_id)` pair or a
    callable invoked with `(source_session_id, prompt, mode)`. `log` records
    every call so tests can assert on the full sequence."""

    responses: list[ScriptedEntry] = field(default_factory=list)
    log: list[tuple[str, str, str]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def respond(self, text: str, session_id: str = "scripted-session") -> "ScriptedChannel":
        with self._lock:
            self.responses.append((text, session_id))
        return self

    def respond_with(self, fn: ScriptedResponse) -> "ScriptedChannel":
        with self._lock:
            self.responses.append(fn)
        return self

    def run_turn(
        self,
        source_session_id: str,
        prompt: str,
        *,
        mode: str,
        cwd: Optional[str] = None,
        timeout: int = 300,
        extra_env: Optional[dict] = None,
        on_session_id: Optional[Callable[[str], None]] = None,
        preallocated_session_id: Optional[str] = None,
    ) -> TurnResult:
        _ = (cwd, timeout, extra_env, preallocated_session_id)  # parity
        with self._lock:
            if not self.responses:
                raise AssertionError(
                    f"ScriptedChannel exhausted; "
                    f"unscripted call: source={source_session_id}, mode={mode}"
                )
            self.log.append((source_session_id, prompt, mode))
            nxt = self.responses.pop(0)
        if callable(nxt):
            result = nxt(source_session_id, prompt, mode)
            if on_session_id is not None and result.session_id:
                _fire_on_session_id(on_session_id, result.session_id)
            return result
        text, session_id = nxt
        if on_session_id is not None and session_id:
            _fire_on_session_id(on_session_id, session_id)
        return TurnResult(
            text=text, session_id=session_id, duration=0.0,
            api_request_id="", input_tokens=0, output_tokens=0,
            cache_read_tokens=0, cache_creation_tokens=0, total_cost_usd=0.0,
        )
