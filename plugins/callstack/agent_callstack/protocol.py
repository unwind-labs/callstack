"""The control envelope: how an agent talks back to the runtime.

A child agent ends its turn by emitting one fenced ```json block. Three opcodes:
  CALL   — spawn a child node
  YIELD  — pause for user input
  RETURN — finish and hand a result to the parent

This module owns the envelope grammar, the system prompt that teaches it,
and the helpers that build prompts for follow-up turns.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional, Union


SYSTEM_INSTRUCTION = """\
You are running in a forked session — a child process that inherited the full \
context of your parent agent. When finished with the task, end your turn by \
emitting EXACTLY ONE JSON envelope wrapped in a fenced ```json code block.

- {"op": "call", "task": "<what to accomplish>"}
- {"op": "yield", "question": "<question for user>"}
- {"op": "return", "result": ..., "summary": "...", "next": "..."}
"""


# ---------- Envelope value types ----------

@dataclass(frozen=True)
class Call:
    task: str

@dataclass(frozen=True)
class Yield:
    question: str

@dataclass(frozen=True)
class Return:
    result: Any = None
    summary: Optional[str] = None
    suggested_next: Optional[str] = None


Envelope = Union[Call, Yield, Return]


# ---------- Parsing ----------

_FENCED_JSON_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)

# Opcodes that constitute a control envelope. Used by parse_envelope to
# distinguish a real envelope from incidental JSON snippets the model
# may emit in prose.
_ENVELOPE_OPS = frozenset({"call", "yield", "return"})


def _fenced_envelope_dicts(output: str) -> list[dict]:
    """All fenced JSON dicts in `output` that look like envelopes (have a
    recognized `op` field), in source order."""
    envelopes: list[dict] = []
    for match in _FENCED_JSON_RE.finditer(output):
        try:
            obj = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("op") in _ENVELOPE_OPS:
            envelopes.append(obj)
    return envelopes


def _last_json_object(output: str) -> Optional[dict]:
    """Fallback: last JSON dict anywhere in `output` when no fenced
    envelope is present. Forward scan via the standard JSON tokenizer
    correctly handles braces inside string literals."""
    decoder = json.JSONDecoder()
    last: Optional[dict] = None
    i, n = 0, len(output)
    while i < n:
        j = output.find("{", i)
        if j == -1:
            break
        try:
            obj, end_idx = decoder.raw_decode(output, j)
            if isinstance(obj, dict):
                last = obj
            i = end_idx
        except json.JSONDecodeError:
            i = j + 1
    return last


def _build_envelope(obj: dict) -> Optional[Envelope]:
    op = obj.get("op")
    if op == "call":
        return Call(task=obj.get("task", ""))
    if op == "yield":
        return Yield(question=obj.get("question", ""))
    if op == "return":
        return Return(
            result=obj.get("result"),
            summary=obj.get("summary"),
            suggested_next=obj.get("next"),
        )
    return None


def parse_envelope(output: str) -> Optional[Envelope]:
    """Parse the agent's last JSON envelope.

    Returns ``None`` on protocol violation: no JSON, unknown opcode, OR
    multiple fenced envelopes with conflicting opcodes (e.g. a YIELD
    followed by a RETURN). The driver treats ``None`` as a turn failure
    rather than collapsing to a silent empty Return.

    Conflict rule (CORR-102): the protocol mandates exactly one envelope.
    A child that emits a YIELD then a RETURN is either confused or
    attempting to hijack control flow — neither warrants honoring the
    last one. Same-opcode duplicates are allowed (treated as model retry)
    and the last is returned.
    """
    envelopes = _fenced_envelope_dicts(output)
    if envelopes:
        ops = {env["op"] for env in envelopes}
        if len(ops) > 1:
            # Mixed opcodes — refuse to guess intent.
            return None
        return _build_envelope(envelopes[-1])

    # No fenced envelope — fall back to bare JSON anywhere in the text
    # (legacy behavior for models that forget the fence).
    obj = _last_json_object(output)
    if obj is None:
        return None
    return _build_envelope(obj)


# ---------- Prompt construction ----------

def starting_prompt(task: str, task_id: Optional[str] = None) -> str:
    tag = f" [{task_id}]" if task_id else ""
    return f"## Starting Task{tag}\n\n" + SYSTEM_INSTRUCTION + f"\nTask: {task}"


def child_returned_prompt(child_result: Any) -> str:
    if child_result is None:
        body = ""
    elif isinstance(child_result, str):
        body = child_result
    else:
        body = json.dumps(child_result, ensure_ascii=False)
    return "Your child completed. Here is the result:\n\n" + body
