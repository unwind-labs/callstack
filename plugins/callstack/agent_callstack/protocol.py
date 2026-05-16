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


def _last_json_object(output: str) -> Optional[dict]:
    """Last JSON dict in `output`, preferring fenced ```json blocks.

    Falls back to a forward scan for any `{...}` substring that the JSON
    decoder accepts — this correctly handles braces inside string literals
    because we use the standard JSON tokenizer, not hand-rolled brace matching.
    """
    for match in reversed(list(_FENCED_JSON_RE.finditer(output))):
        try:
            obj = json.loads(match.group(1))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue

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


def parse_envelope(output: str) -> Optional[Envelope]:
    """Parse the agent's last JSON envelope.

    Returns ``None`` when no JSON object was found OR when an unknown opcode
    is present — the caller (driver) treats this as a turn failure rather
    than a successful empty Return. An explicit ``{"op":"return"}`` with no
    ``result`` is still a legitimate empty success and returns ``Return()``.
    """
    obj = _last_json_object(output)
    if obj is None:
        return None
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
