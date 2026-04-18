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
You are running in a forked session — a child process that inherited the full context of \
your parent agent. Execute the task below.

End your turn by emitting EXACTLY ONE JSON envelope wrapped in a fenced \
```json code block. That envelope is how you communicate back to the \
runtime. Any other fenced JSON earlier in your response is ignored; only \
the last one is parsed. Pick exactly one of three operations:

1) CALL — hand off work to a child process.
```json
{"op": "call", "task": "<what to accomplish>"}
```
Use CALL when the task ahead is multi-step, may involve its own chain of \
calls or user interaction, and you want only the result back — not the \
intermediate work. The child inherits your full context. Its execution \
trace is discarded; only its return value comes back to you. Do your own \
work first, then CALL when you reach a point requiring a child. Don't \
CALL simple things you can do in one or two tool calls.

2) YIELD — pause for user input.
```json
{"op": "yield", "question": "<question for user>"}
```
Only when you MUST have information that only the user can provide (e.g. \
MFA codes, passwords, confirmations). Do not guess.

3) RETURN — finish and hand results to the parent.
```json
{"op": "return", "result": "...", "summary": "...", "next": "..."}
```
- `result` — the deliverable/answer for the parent. Structure it however \
is appropriate for the task.
- `summary` — COMPACT brain-dump of everything the parent needs to \
execute upcoming tasks: sub-calls made and their outcomes, key decisions \
and assumptions, side effects (files touched, commands run, external \
state changed), dead ends not worth retrying. Optimize for tokens — \
terse bullets or prose, no filler. The parent should NOT need to read \
your session log. Omit this field or set null if there is genuinely \
nothing beyond `result` worth carrying forward.
- `next` — advisory one-line suggestion for what should happen next. \
Optional. The parent has broader context and decides; this just aligns \
your summary toward what matters.
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


def parse_envelope(output: str) -> Envelope:
    """Parse the agent's last JSON envelope. Missing/unknown → empty Return."""
    obj = _last_json_object(output)
    if obj is None:
        return Return()
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
    return Return()


# ---------- Prompt construction ----------

def starting_prompt(task: str, task_id: Optional[str] = None) -> str:
    tag = f" [{task_id}]" if task_id else ""
    return SYSTEM_INSTRUCTION + f"\n\n## Starting Task{tag}\n\n" + task


def child_returned_prompt(child_result: Any) -> str:
    return "Your child completed. Here is the result:\n\n" + _stringify(child_result)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)
