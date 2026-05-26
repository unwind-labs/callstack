"""Post-execution inspection of session traces.

One `SessionAnalyzer` class exposes structured data about a completed (or
in-progress) callstack run. The CLI scripts in `resources/analysis/` are
thin formatters over these methods.

What it consolidates from the previous four scripts:
- Reading the call_trace.jsonl entries that the runtime appends per turn.
- Parsing session JSONL files (Claude Code's per-session message log).
- Reconstructing the parent → child call tree by walking session JSONL
  metadata (each forked session records its parent session id).
- Format helpers (durations, sizes, tree rendering).
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from .session import PROJECTS_DIR


# ---------- Value types ----------

@dataclass(frozen=True)
class TraceEvent:
    """One entry in call_trace.jsonl (written by TraceWriter)."""
    timestamp: Optional[datetime]
    depth: int
    session_id: str
    task: str
    duration: float
    result_length: int
    error: Optional[str]


@dataclass(frozen=True)
class SessionMessage:
    """One line from a session JSONL."""
    timestamp: Optional[datetime]
    type: str
    role: Optional[str]
    text: str
    tool_name: Optional[str]


@dataclass
class CallNode:
    """Reconstructed call tree node."""
    session_id: str
    task: str
    depth: int
    duration: float
    children: list["CallNode"] = field(default_factory=list)
    error: Optional[str] = None


@dataclass(frozen=True)
class SessionStats:
    """Summary numbers for one session JSONL."""
    message_count: int
    by_type: dict[str, int]
    duration: float
    first_timestamp: Optional[datetime]
    last_timestamp: Optional[datetime]


# ---------- Analyzer ----------

class SessionAnalyzer:
    """Reads session JSONL + call_trace.jsonl files and exposes structured views.

    Construct with the projects directory (defaults to ~/.claude/projects).
    Pure read-only — never mutates anything on disk."""

    def __init__(self, projects_dir: Path = PROJECTS_DIR):
        self._projects_dir = projects_dir

    # ---- trace files (call_trace.jsonl) ----

    def trace_events(self, trace_file: Path) -> list[TraceEvent]:
        return [
            TraceEvent(
                timestamp=_parse_ts(d.get("timestamp")),
                depth=d.get("call_depth", 0),
                session_id=d.get("session_id", ""),
                task=d.get("task", ""),
                duration=d.get("duration_seconds", 0.0),
                result_length=d.get("result_length", 0),
                error=d.get("error"),
            )
            for d in _iter_jsonl(trace_file)
        ]

    # ---- session files (per-session JSONL) ----

    def session_messages(self, session_file: Path) -> list[SessionMessage]:
        out: list[SessionMessage] = []
        for obj in _iter_jsonl(session_file):
            text, tool = _content_preview(obj)
            out.append(SessionMessage(
                timestamp=_parse_ts(obj.get("timestamp")),
                type=obj.get("type", ""),
                role=obj.get("message", {}).get("role") if isinstance(obj.get("message"), dict) else None,
                text=text,
                tool_name=tool,
            ))
        return out

    def session_stats(self, session_file: Path) -> SessionStats:
        # Streams the file rather than calling session_messages(): a session
        # JSONL can be many MB, and stats only need per-message type + timestamp,
        # so we accumulate counts/min/max in O(1) message memory instead of
        # materializing the full SessionMessage list.
        by_type: dict[str, int] = defaultdict(int)
        count = 0
        first: Optional[datetime] = None
        last: Optional[datetime] = None
        for obj in _iter_jsonl(session_file):
            count += 1
            by_type[obj.get("type", "")] += 1
            ts = _parse_ts(obj.get("timestamp"))
            if ts is not None:
                if first is None or ts < first:
                    first = ts
                if last is None or ts > last:
                    last = ts
        duration = (last - first).total_seconds() if first and last else 0.0
        return SessionStats(
            message_count=count,
            by_type=dict(by_type),
            duration=duration,
            first_timestamp=first,
            last_timestamp=last,
        )

    def parent_session_id(self, session_file: Path) -> Optional[str]:
        """A forked session records its parent in early metadata. Returns it."""
        if not session_file.exists():
            return None
        with open(session_file, "r") as f:
            for i, line in enumerate(f):
                if i > 50:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                pid = obj.get("parentSessionId") or obj.get("parent_session_id")
                if pid:
                    return pid
        return None

    # ---- call tree reconstruction ----

    def build_tree(self, trace_file: Path,
                   root_session: Optional[str] = None) -> Optional[CallNode]:
        """Reconstruct the call tree from a call_trace.jsonl + session JSONL parents.

        Each TraceEvent groups by session_id; sessions are linked into a tree
        via parent_session_id read from the session JSONL files. If
        `root_session` is None, picks the most recent root."""
        events = self.trace_events(trace_file)
        if not events:
            return None

        # Group entries by session id
        by_sess: dict[str, list[TraceEvent]] = defaultdict(list)
        for e in events:
            by_sess[e.session_id].append(e)

        # For each session, find its parent (None = root).
        delta_dir = trace_file.parent
        parents: dict[str, Optional[str]] = {}
        for sid in by_sess:
            sf = delta_dir / f"{sid}.jsonl"
            parents[sid] = self.parent_session_id(sf) if sf.exists() else None

        # Pick a root.
        if root_session is None:
            roots = [s for s, p in parents.items() if p not in by_sess]
            if not roots:
                return None
            # Most recent root by max event timestamp.
            roots.sort(
                key=lambda s: max((e.timestamp or datetime.min) for e in by_sess[s]),
                reverse=True,
            )
            root_session = roots[0]

        # Children index.
        children_of: dict[str, list[str]] = defaultdict(list)
        for sid, parent in parents.items():
            if parent and parent in by_sess:
                children_of[parent].append(sid)

        def build(sid: str, depth: int) -> CallNode:
            sess_events = by_sess[sid]
            first = sess_events[0]
            duration = sum(e.duration for e in sess_events)
            errors = [e.error for e in sess_events if e.error]
            node = CallNode(
                session_id=sid, task=first.task, depth=depth,
                duration=duration, error=errors[0] if errors else None,
            )
            for child_sid in children_of.get(sid, []):
                node.children.append(build(child_sid, depth + 1))
            return node

        return build(root_session, 0)


# ---------- Format helpers ----------

def format_duration(seconds: float) -> str:
    if seconds < 0.1:
        return f"{seconds*1000:.0f}ms"
    if seconds < 1:
        return f"{seconds:.2f}s"
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(seconds, 60)
    return f"{int(m)}m{s:.0f}s"


def format_size(nbytes: int) -> str:
    return f"{nbytes}B" if nbytes < 1024 else f"{nbytes / 1024:.0f}KB"


def format_tree(root: CallNode, *, indent: int = 0) -> str:
    """Render a CallNode as an ASCII tree."""
    pad = "  " * indent
    badge = f" ❌ {root.error}" if root.error else ""
    lines = [f"{pad}└─ {root.session_id[:8]} ({format_duration(root.duration)}) "
             f"{root.task[:80]}{badge}"]
    for c in root.children:
        lines.append(format_tree(c, indent=indent + 1))
    return "\n".join(lines)


# ---------- internal helpers ----------

def _iter_jsonl(path: Path) -> Iterator[dict]:
    """Yield parsed JSON objects from a JSONL file, one line at a time.

    Streams the file (open + iterate) rather than read_text().splitlines(),
    so a multi-MB session/trace log is never held in memory as one big string
    plus a list of every line. Missing file -> empty iterator; blank and
    unparseable lines are skipped (these logs are appended to live and may
    have a torn final line)."""
    if not path.exists():
        return
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.rstrip("Z"))
    except (ValueError, TypeError):
        return None


def _content_preview(obj: dict) -> tuple[str, Optional[str]]:
    """Extract a flat text preview and a tool name (if any) from a message."""
    msg = obj.get("message") or {}
    if isinstance(msg.get("content"), str):
        return msg["content"][:200], None
    if isinstance(msg.get("content"), list):
        for block in msg["content"]:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                return block.get("text", "")[:200], None
            if block.get("type") == "tool_use":
                return "", block.get("name")
            if block.get("type") == "tool_result":
                c = block.get("content")
                if isinstance(c, str):
                    return c[:200], None
                return "", None
    return "", None
