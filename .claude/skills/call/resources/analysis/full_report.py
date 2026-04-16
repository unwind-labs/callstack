#!/usr/bin/env python3
"""
full_report.py — Full analysis report for a /forked session session tree.

Produces a unified chronological event timeline across all sessions,
a "where the time goes" category breakdown, and context growth summary.

Usage:
    python3 full_report.py                                     # auto-detect, latest run
    python3 full_report.py --root <session_id>                 # specific root
    python3 full_report.py -v                                  # verbose (more events)
    python3 full_report.py /path/to/project_dir                # explicit project dir

Examples:
    python3 .claude/skills/call/resources/analysis/full_report.py

    python3 .claude/skills/call/resources/analysis/full_report.py \\
        ~/.claude/projects/-Users-amolk-work-call-agents-examples-customer-support \\
        --root fff6800e -v
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def parse_timestamp(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.rstrip("Z"))
    except (ValueError, TypeError):
        return None


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
    if nbytes < 1024:
        return f"{nbytes}B"
    return f"{nbytes / 1024:.0f}KB"


def format_time(dt: datetime) -> str:
    """Format datetime as HH:MM:SS for the timeline."""
    return dt.strftime("%H:%M:%S")


def get_raw_content(obj: dict) -> str:
    """Extract raw content string (with newlines) from a message object
    or queue-operation content."""
    if obj.get("type") == "queue-operation":
        return obj.get("content", "")
    if "message" in obj and "content" in obj["message"]:
        c = obj["message"]["content"]
        if isinstance(c, str):
            return c
    return ""


def get_content_preview(obj: dict) -> tuple[str, str | None]:
    """Extract flattened content preview and tool name."""
    content = ""
    tool_name = None

    if obj.get("type") == "queue-operation":
        qc = obj.get("content", "")
        if qc:
            content = qc[:300].replace("\n", " ")
        return content, None

    if "message" not in obj or "content" not in obj["message"]:
        return "", None
    c = obj["message"]["content"]
    if isinstance(c, str):
        content = c[:300].replace("\n", " ")
    elif isinstance(c, list):
        for block in c:
            if not isinstance(block, dict):
                continue
            bt = block.get("type", "")
            if bt == "text" and not content:
                content = block.get("text", "")[:300].replace("\n", " ")
            elif bt == "tool_use":
                tool_name = block.get("name", "?")
                if not content:
                    content = f"[tool: {tool_name}]"
    return content, tool_name


def get_tool_inputs(obj: dict) -> dict[str, dict]:
    """Extract tool_use input dicts keyed by tool name."""
    inputs = {}
    if "message" not in obj or "content" not in obj["message"]:
        return inputs
    c = obj["message"]["content"]
    if isinstance(c, list):
        for block in c:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name", "")
                inputs[name] = block.get("input", {})
    return inputs


# ---------------------------------------------------------------------------
# Session tree reconstruction
# ---------------------------------------------------------------------------

def find_parent_session(session_id: str, delta_dir: Path) -> str | None:
    """Find parent session by matching 'Forked session:' to this session ID."""
    delta_file = delta_dir / f"{session_id}.delta.jsonl"
    if not delta_file.exists():
        return None
    last_parent = None
    matched_parent = None
    with open(delta_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                raw = get_raw_content(obj)
                if "Forking session for /call" in raw and "Parent session:" in raw:
                    parent_in_block = None
                    forked_id_in_block = None
                    for part in raw.split("\n"):
                        p = part.strip()
                        if p.startswith("Parent session:"):
                            parent_in_block = p.split(":", 1)[1].strip()
                        elif p.startswith("Forked session:"):
                            forked_id_in_block = p.split(":", 1)[1].strip()
                    last_parent = parent_in_block
                    if forked_id_in_block and forked_id_in_block.startswith(session_id[:8]):
                        matched_parent = parent_in_block
            except (json.JSONDecodeError, KeyError):
                continue
    return matched_parent or last_parent


def load_traces(trace_file: Path) -> list[dict]:
    entries = []
    with open(trace_file) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def resolve_prefix(prefix: str, all_known: set[str]) -> str | None:
    for sid in all_known:
        if sid == prefix or sid.startswith(prefix):
            return sid
    return None


def build_tree(entries: list[dict], delta_dir: Path,
               root_session: str | None = None) -> dict:
    by_session = defaultdict(list)
    for e in entries:
        by_session[e["session_id"]].append(e)

    all_sessions = set(by_session.keys())
    parent_map = {}
    children_map = defaultdict(list)
    for sid in all_sessions:
        parent = find_parent_session(sid, delta_dir)
        if parent:
            parent_map[sid] = parent
            children_map[parent].append(sid)

    all_known = set(all_sessions) | set(children_map.keys())

    if root_session:
        resolved = resolve_prefix(root_session, all_known)
        if resolved:
            root_session = resolved
        else:
            return {}
        roots = [root_session]
    else:
        roots = [sid for sid in all_sessions if sid not in parent_map]

    return {
        "by_session": by_session,
        "parent_map": parent_map,
        "children_map": children_map,
        "roots": roots,
    }


def get_latest_root(entries: list[dict], delta_dir: Path) -> str | None:
    if not entries:
        return None
    depth1 = [e for e in entries if e.get("call_depth") == 1]
    if not depth1:
        return None
    latest = max(depth1, key=lambda e: e["timestamp"])
    sid = latest["session_id"]
    parent = find_parent_session(sid, delta_dir)
    return parent or sid


# ---------------------------------------------------------------------------
# Event extraction — build a unified timeline from all delta files
# ---------------------------------------------------------------------------

def extract_events(delta_file: Path, session_id: str) -> list[dict]:
    """Extract significant events from a delta file.

    Returns a list of event dicts:
        {ts: datetime, event: str, session_id: str, depth: int, category: str}

    category is one of: thinking, tool_call, mcp_call, skill_load,
                         fork_overhead, child_call, waiting_for_user
    """
    if not delta_file.exists():
        return []

    events = []
    post_fork = False
    task_received = False
    depth = 0
    waiting_for_user = False

    # Two-pass: first collect all timed messages, then derive events
    timed_messages = []

    with open(delta_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = obj.get("type", "?")
            ts = parse_timestamp(obj.get("timestamp", ""))
            raw = get_raw_content(obj)
            content, tool_name = get_content_preview(obj)
            tool_inputs = get_tool_inputs(obj)

            is_fork = False
            is_call_exec = False

            if "Forking session for /call" in raw:
                forked_id = None
                parent_id = None
                depth_val = None
                for part in raw.split("\n"):
                    p = part.strip()
                    if p.startswith("Parent session:"):
                        parent_id = p.split(":", 1)[1].strip()
                    elif p.startswith("Forked session:"):
                        forked_id = p.split(":", 1)[1].strip()
                    elif p.startswith("Fork depth:"):
                        try:
                            depth_val = int(p.split(":", 1)[1].strip())
                        except ValueError:
                            pass

                is_own = (forked_id and forked_id.startswith(session_id[:8])
                          ) if forked_id else True

                if is_own:
                    is_fork = True
                    post_fork = True
                    task_received = False
                    timed_messages.clear()
                    if depth_val is not None:
                        depth = depth_val

            if content.startswith("[/call executed:"):
                is_call_exec = True

            # Detect task prompt
            is_task = False
            task_text = ""
            if post_fork and not task_received:
                if "## Task" in raw:
                    task_text = raw.split("## Task")[-1].strip()
                    first_line = task_text.split("\n")[0].strip().lstrip(" -\u2014#")
                    if first_line:
                        task_text = first_line[:120]
                if "You are running in a forked session" in raw or "## Task" in raw:
                    task_received = True
                    is_task = True

            if ts and post_fork:
                timed_messages.append({
                    "ts": ts, "type": msg_type, "tool": tool_name,
                    "content": content[:200], "raw": raw[:500],
                    "tool_inputs": tool_inputs,
                    "is_fork": is_fork, "is_call_exec": is_call_exec,
                    "is_task": is_task, "task_text": task_text,
                    "depth": depth, "session_id": session_id,
                })

    # Now generate events from the message sequence
    for msg in timed_messages:
        ts = msg["ts"]

        # Fork start
        if msg["is_fork"]:
            events.append({
                "ts": ts,
                "event": f"Fork ({session_id[:8]}) starts",
                "session_id": session_id,
                "depth": msg["depth"],
                "category": "fork_overhead",
            })
            continue

        # Task received
        if msg["is_task"]:
            task_label = msg["task_text"] or "receives task prompt"
            events.append({
                "ts": ts,
                "event": f"Receives task",
                "session_id": session_id,
                "depth": msg["depth"],
                "category": "fork_overhead",
                "duration_note": "",
            })
            continue

        # NEED_INPUT
        if "---NEED_INPUT---" in msg["content"] and msg["type"] == "assistant":
            events.append({
                "ts": ts,
                "event": "Emits ---NEED_INPUT--- (needs user input)",
                "session_id": session_id,
                "depth": msg["depth"],
                "category": "waiting_for_user",
            })
            waiting_for_user = True
            continue

        # USER REPLY relayed back
        if "---USER REPLY---" in msg["content"]:
            events.append({
                "ts": ts,
                "event": "Receives user reply (relayed)",
                "session_id": session_id,
                "depth": msg["depth"],
                "category": "waiting_for_user",
            })
            waiting_for_user = False
            continue

        # RESULT
        if "---RESULT---" in msg["content"] and msg["type"] == "assistant":
            events.append({
                "ts": ts,
                "event": "Emits ---RESULT--- (done)",
                "session_id": session_id,
                "depth": msg["depth"],
                "category": "thinking",
            })
            continue

        # Skill invocation
        if msg["tool"] == "Skill":
            skill_name = ""
            # Extract from tool_use input block (most reliable)
            skill_input = msg.get("tool_inputs", {}).get("Skill", {})
            if isinstance(skill_input, dict):
                skill_name = skill_input.get("skill", "")
            if skill_name:
                events.append({
                    "ts": ts,
                    "event": f"Invokes /{skill_name} skill",
                    "session_id": session_id,
                    "depth": msg["depth"],
                    "category": "skill_load",
                })
            else:
                events.append({
                    "ts": ts,
                    "event": "Invokes skill",
                    "session_id": session_id,
                    "depth": msg["depth"],
                    "category": "skill_load",
                })
            continue

        # ToolSearch
        if msg["tool"] == "ToolSearch":
            events.append({
                "ts": ts,
                "event": "Calls ToolSearch for MCP tools",
                "session_id": session_id,
                "depth": msg["depth"],
                "category": "tool_call",
            })
            continue

        # MCP tool call
        if msg["tool"] and msg["tool"].startswith("mcp__"):
            short_name = msg["tool"].split("__")[-1]
            events.append({
                "ts": ts,
                "event": f"Calls {short_name}",
                "session_id": session_id,
                "depth": msg["depth"],
                "category": "mcp_call",
            })
            continue

        # Child /call invocation
        if msg["tool"] == "Bash" and "callstack" in msg["content"]:
            events.append({
                "ts": ts,
                "event": "Invokes /call (child agent)",
                "session_id": session_id,
                "depth": msg["depth"],
                "category": "child_call",
            })
            continue

        # /call executed marker
        if msg["is_call_exec"]:
            events.append({
                "ts": ts,
                "event": "Child /call returns result",
                "session_id": session_id,
                "depth": msg["depth"],
                "category": "child_call",
            })
            continue

        # Other Bash tool
        if msg["tool"] == "Bash":
            events.append({
                "ts": ts,
                "event": f"Runs Bash command",
                "session_id": session_id,
                "depth": msg["depth"],
                "category": "tool_call",
            })
            continue

        # Read/Glob/Grep/Edit tools
        if msg["tool"] in ("Read", "Glob", "Grep", "Edit", "Write"):
            events.append({
                "ts": ts,
                "event": f"Calls {msg['tool']}",
                "session_id": session_id,
                "depth": msg["depth"],
                "category": "tool_call",
            })
            continue

        # Other tool
        if msg["tool"]:
            events.append({
                "ts": ts,
                "event": f"Calls {msg['tool']}",
                "session_id": session_id,
                "depth": msg["depth"],
                "category": "tool_call",
            })
            continue

        # Assistant thinking/responding (only include in verbose mode via flag)
        if msg["type"] == "assistant":
            # Summarize what the assistant is doing
            snippet = msg["content"][:80].strip()
            if snippet:
                events.append({
                    "ts": ts,
                    "event": f"Thinking: {snippet}",
                    "session_id": session_id,
                    "depth": msg["depth"],
                    "category": "thinking",
                    "_verbose": True,  # only show in verbose mode
                })
            continue

    return events


# ---------------------------------------------------------------------------
# Timing analysis (category aggregation from events)
# ---------------------------------------------------------------------------

def analyze_delta_timing(delta_file: Path, session_id: str) -> dict:
    """Analyze timing from a delta file. Returns a dict of timing info."""
    if not delta_file.exists():
        return {}

    file_size = delta_file.stat().st_size
    fork_start = None
    task_start = None
    first_response = None
    need_input_time = None
    result_time = None
    parent_session = None
    depth = 0
    task = ""
    post_fork = False
    task_received = False

    timed_messages = []

    with open(delta_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = obj.get("type", "?")
            ts = parse_timestamp(obj.get("timestamp", ""))
            raw = get_raw_content(obj)
            content, tool_name = get_content_preview(obj)

            is_fork = False
            is_call_exec = False

            if "Forking session for /call" in raw:
                forked_id = None
                parent_id = None
                depth_val = None
                for part in raw.split("\n"):
                    p = part.strip()
                    if p.startswith("Parent session:"):
                        parent_id = p.split(":", 1)[1].strip()
                    elif p.startswith("Forked session:"):
                        forked_id = p.split(":", 1)[1].strip()
                    elif p.startswith("Fork depth:"):
                        try:
                            depth_val = int(p.split(":", 1)[1].strip())
                        except ValueError:
                            pass

                is_own = (forked_id and forked_id.startswith(session_id[:8])
                          ) if forked_id else True

                if is_own:
                    is_fork = True
                    post_fork = True
                    task_received = False
                    fork_start = ts
                    task_start = None
                    first_response = None
                    need_input_time = None
                    result_time = None
                    timed_messages.clear()
                    if parent_id:
                        parent_session = parent_id
                    if depth_val is not None:
                        depth = depth_val

            if content.startswith("[/call executed:"):
                is_call_exec = True

            if post_fork and not task_received:
                if "## Task" in raw:
                    task_text = raw.split("## Task")[-1].strip()
                    first_line = task_text.split("\n")[0].strip()
                    first_line = first_line.lstrip(" -\u2014#")
                    if first_line:
                        task = first_line[:120]
                if "You are running in a forked session" in raw or "## Task" in raw:
                    task_received = True
                    task_start = ts

            if (task_received and msg_type == "assistant" and ts
                    and first_response is None
                    and content and not content.startswith("No response")):
                first_response = ts

            if "---NEED_INPUT---" in content and msg_type == "assistant":
                need_input_time = ts
            if "---RESULT---" in content and msg_type == "assistant":
                result_time = ts

            if ts and post_fork:
                timed_messages.append({
                    "ts": ts, "type": msg_type, "tool": tool_name,
                    "content": content[:80], "is_fork": is_fork,
                    "is_call_exec": is_call_exec,
                    "is_task": "You are running in a forked session" in content,
                })

    # Build time segments from consecutive timed messages
    by_category = defaultdict(float)
    waiting_for_user = False
    for j in range(len(timed_messages) - 1):
        msg = timed_messages[j]
        nxt = timed_messages[j + 1]
        dur = (nxt["ts"] - msg["ts"]).total_seconds()
        if dur < 0.01:
            continue

        if "---NEED_INPUT---" in msg["content"]:
            waiting_for_user = True

        if msg["is_fork"] or msg["is_call_exec"]:
            cat = "fork_overhead"
        elif waiting_for_user:
            cat = "waiting_for_user"
        elif msg["tool"] == "Bash" and "callstack" in msg["content"]:
            cat = "child_call"
        elif msg["tool"] == "Bash":
            cat = "tool_call"
        elif msg["tool"] == "Skill":
            cat = "skill_load"
        elif msg["tool"] == "ToolSearch":
            cat = "tool_call"
        elif msg["tool"] and msg["tool"].startswith("mcp__"):
            cat = "mcp_call"
        elif msg["tool"]:
            cat = "tool_call"
        elif msg["type"] == "assistant":
            cat = "thinking"
        else:
            cat = "api_roundtrip"

        if "---USER REPLY---" in msg["content"] or "---USER REPLY---" in nxt["content"]:
            waiting_for_user = False

        by_category[cat] += dur

    startup = None
    if fork_start and first_response:
        startup = (first_response - fork_start).total_seconds()

    own_time = None
    end = need_input_time or result_time
    if task_start and end:
        own_time = (end - task_start).total_seconds()

    return {
        "session_id": session_id,
        "file_size": file_size,
        "depth": depth,
        "task": task,
        "parent_session": parent_session,
        "startup": startup,
        "own_time": own_time,
        "by_category": dict(by_category),
    }


# ---------------------------------------------------------------------------
# Box-drawing table renderer
# ---------------------------------------------------------------------------

def render_table(headers: list[str], rows: list[list[str]],
                 col_widths: list[int] | None = None) -> str:
    """Render a table with box-drawing characters.

    Each row is a list of cell strings. A row that is None inserts a
    separator line (├─┼─┤).
    """
    if not col_widths:
        col_widths = [len(h) for h in headers]
        for row in rows:
            if row is None:
                continue
            for i, cell in enumerate(row):
                if i < len(col_widths):
                    col_widths[i] = max(col_widths[i], len(cell))

    def top_border():
        segs = ["\u2500" * (w + 2) for w in col_widths]
        return "\u250c" + "\u252c".join(segs) + "\u2510"

    def mid_border():
        segs = ["\u2500" * (w + 2) for w in col_widths]
        return "\u251c" + "\u253c".join(segs) + "\u2524"

    def bot_border():
        segs = ["\u2500" * (w + 2) for w in col_widths]
        return "\u2514" + "\u2534".join(segs) + "\u2518"

    def data_row(cells):
        parts = []
        for i, w in enumerate(col_widths):
            cell = cells[i] if i < len(cells) else ""
            parts.append(f" {cell:<{w}s} ")
        return "\u2502" + "\u2502".join(parts) + "\u2502"

    lines = []
    lines.append(top_border())
    lines.append(data_row(headers))
    lines.append(mid_border())
    for row in rows:
        if row is None:
            lines.append(mid_border())
        else:
            lines.append(data_row(row))
    lines.append(bot_border())
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Session label assignment
# ---------------------------------------------------------------------------

def assign_session_labels(timings: list[dict], root_id: str) -> dict[str, str]:
    """Assign human labels like 'root', 'L1', 'L2a', 'L2b' to sessions."""
    labels = {root_id: "root"}

    # Group timings by depth
    by_depth: dict[int, list[dict]] = defaultdict(list)
    for t in timings:
        by_depth[t.get("depth", 0)].append(t)

    for d in sorted(by_depth.keys()):
        sessions = by_depth[d]
        if len(sessions) == 1:
            sid = sessions[0]["session_id"]
            if sid not in labels:
                labels[sid] = f"L{d}"
        else:
            # Multiple sessions at same depth — append a, b, c
            sessions.sort(key=lambda t: t.get("startup") or 0)
            for i, t in enumerate(sessions):
                sid = t["session_id"]
                if sid not in labels:
                    suffix = chr(ord('a') + i) if len(sessions) > 1 else ""
                    labels[sid] = f"L{d}{suffix}"

    return labels


# ---------------------------------------------------------------------------
# Report: Event Timeline
# ---------------------------------------------------------------------------

def print_event_timeline(all_events: list[dict], labels: dict[str, str],
                         verbose: bool = False):
    """Print the unified chronological event timeline table."""
    # Filter events
    events = sorted(all_events, key=lambda e: e["ts"])
    if not verbose:
        events = [e for e in events if not e.get("_verbose")]

    if not events:
        print("\n  (no events found)")
        return

    # Compute durations between consecutive events
    rows = []
    for i, ev in enumerate(events):
        time_str = format_time(ev["ts"])
        event_str = ev["event"]
        session_str = labels.get(ev["session_id"], ev["session_id"][:8])

        # Duration: time until next event, annotated with what consumed it
        dur_str = ""
        if i < len(events) - 1:
            delta = (events[i + 1]["ts"] - ev["ts"]).total_seconds()
            if delta >= 0.5:
                dur_str = format_duration(delta)
                # Add a category hint
                cat = ev.get("category", "")
                if cat == "thinking":
                    dur_str += " thinking"
                elif cat == "fork_overhead":
                    dur_str += " startup"
                elif cat == "child_call":
                    dur_str += " child"
                elif cat == "waiting_for_user":
                    dur_str += " waiting"
                elif cat in ("tool_call", "mcp_call"):
                    dur_str += " call"
                elif cat == "skill_load":
                    dur_str += " loading"

        rows.append([time_str, event_str, session_str, dur_str])

    # Determine first and last timestamps for the header
    first_ts = events[0]["ts"]
    last_ts = events[-1]["ts"]
    wall_clock = (last_ts - first_ts).total_seconds()

    print(f"\n  End-to-end: ~{format_duration(wall_clock)}"
          f" ({format_time(first_ts)} \u2192 {format_time(last_ts)})")

    headers = ["Time", "Event", "Session", "Duration"]
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))

    # Cap event column to avoid super-wide tables
    col_widths[1] = min(col_widths[1], 55)

    print()
    table = render_table(headers, rows, col_widths)
    # Indent each line
    for line in table.split("\n"):
        print(f"  {line}")


# ---------------------------------------------------------------------------
# Report: Where the Time Goes
# ---------------------------------------------------------------------------

SUMMARY_CAT_ORDER = [
    "fork_overhead", "thinking", "tool_call", "mcp_call",
    "skill_load", "child_call", "waiting_for_user",
]

SUMMARY_CAT_LABELS = {
    "fork_overhead": "Session clone + startup",
    "thinking": "Model thinking (reading skills, planning)",
    "api_roundtrip": "Model thinking (reading skills, planning)",
    "tool_call": "Tool calls (Bash, ToolSearch, Read, etc.)",
    "mcp_call": "MCP calls (backend tools)",
    "skill_load": "Skill loading",
    "child_call": "Child agent calls",
    "waiting_for_user": "Waiting for user input",
}


def print_where_time_goes(timings: list[dict]):
    """Print the 'Where the time goes' summary table."""
    total_by_cat = defaultdict(float)
    for t in timings:
        for cat, dur in t.get("by_category", {}).items():
            # Don't double-count child_call — it's wall-clock overlap
            if cat != "child_call":
                total_by_cat[cat] += dur

    # Merge api_roundtrip into thinking
    if "api_roundtrip" in total_by_cat:
        total_by_cat["thinking"] += total_by_cat.pop("api_roundtrip")

    grand_total = sum(total_by_cat.values())
    if grand_total < 0.1:
        return

    print("\n  Where the time goes:\n")

    rows = []
    for cat in SUMMARY_CAT_ORDER:
        dur = total_by_cat.get(cat, 0)
        if dur < 0.1:
            continue
        label = SUMMARY_CAT_LABELS.get(cat, cat)
        pct = dur / grand_total * 100
        rows.append([label, f"~{format_duration(dur)}", f"{pct:.0f}%"])

    headers = ["Category", "Time", "%"]
    table = render_table(headers, rows)
    for line in table.split("\n"):
        print(f"  {line}")


# ---------------------------------------------------------------------------
# Report: Context Growth
# ---------------------------------------------------------------------------

def print_context_growth(timings: list[dict], labels: dict[str, str]):
    """Print context size growth across session depths."""
    if not timings:
        return

    sorted_timings = sorted(timings, key=lambda t: t.get("depth", 0))

    print("\n  Context growth:")
    for t in sorted_timings:
        sid = t["session_id"]
        label = labels.get(sid, sid[:8])
        size = format_size(t.get("file_size", 0))
        depth = t.get("depth", 0)
        print(f"    {label} (depth {depth}): {size}")


# ---------------------------------------------------------------------------
# Report: Call Tree (compact)
# ---------------------------------------------------------------------------

def print_call_tree(tree: dict, node: str, labels: dict[str, str],
                    indent: int = 0, is_last: bool = True):
    by_session = tree["by_session"]
    children_map = tree["children_map"]
    entries = by_session.get(node, [])
    label = labels.get(node, node[:8])

    if indent == 0:
        branch = ""
    else:
        branch = "\u2502   " * (indent - 1) + ("\u2514\u2500\u2500 " if is_last else "\u251c\u2500\u2500 ")

    if entries:
        first = entries[0]
        task = first.get("task", "?")
        if len(task) > 60:
            task = task[:57] + "..."
        total_dur = sum(e["duration_seconds"] for e in entries)
        error = any(e.get("error") for e in entries)
        status = " ERR" if error else ""
        print(f"  {branch}[{label}] {format_duration(total_dur)}{status}  {task}")
    else:
        print(f"  {branch}[{label}] (root session)")

    children = children_map.get(node, [])
    children.sort(key=lambda s: by_session.get(s, [{}])[0].get("timestamp", ""))
    for i, child in enumerate(children):
        print_call_tree(tree, child, labels, indent + 1,
                        is_last=(i == len(children) - 1))


# ---------------------------------------------------------------------------
# Project directory discovery
# ---------------------------------------------------------------------------

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"


def _normalize(s: str) -> str:
    import re
    return re.sub(r'[/_\-]+', '-', s).strip('-').lower()


def discover_project_dir(cwd: str | None = None) -> Path | None:
    cwd = cwd or os.getcwd()
    cwd_norm = _normalize(cwd)

    if not PROJECTS_DIR.is_dir():
        return None

    for d in PROJECTS_DIR.iterdir():
        if d.is_dir() and d.name != "memory":
            if _normalize(d.name) == cwd_norm:
                return d

    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Full analysis report for /forked session session trees",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          # auto-detect project from cwd, latest run
  %(prog)s --root fff6800e          # specific root session
  %(prog)s -v                       # verbose (include thinking events)
  %(prog)s /path/to/project_dir     # explicit project dir
""")
    parser.add_argument("project_dir", nargs="?", default=None,
                        help="Claude project directory (auto-detected from cwd if omitted)")
    parser.add_argument("--root", help="Root session ID (prefix OK)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show verbose events (including thinking)")
    args = parser.parse_args()

    if args.project_dir:
        project_dir = Path(args.project_dir)
    else:
        project_dir = discover_project_dir()
        if project_dir is None:
            print("Could not auto-detect project directory from cwd.", file=sys.stderr)
            print(f"Run from your project directory, or pass the path explicitly:", file=sys.stderr)
            print(f"  {sys.argv[0]} ~/.claude/projects/<encoded-path>", file=sys.stderr)
            sys.exit(1)
        print(f"Project: {project_dir.name}")

    trace_dir = project_dir / "call_traces"
    trace_file = trace_dir / "call_trace.jsonl"

    if not trace_file.exists():
        print(f"No call_trace.jsonl in {trace_dir}", file=sys.stderr)
        sys.exit(1)

    entries = load_traces(trace_file)
    if not entries:
        print("No trace entries", file=sys.stderr)
        sys.exit(1)

    root_id = args.root
    if not root_id:
        root_id = get_latest_root(entries, trace_dir)

    if not root_id:
        print("No root found. Specify --root <id>", file=sys.stderr)
        sys.exit(1)

    # Resolve prefix
    all_deltas = list(trace_dir.glob("*.delta.jsonl"))
    all_known = set()
    for df in all_deltas:
        sid = df.stem.replace(".delta", "")
        all_known.add(sid)
        parent = find_parent_session(sid, trace_dir)
        if parent:
            all_known.add(parent)

    resolved = resolve_prefix(root_id, all_known)
    if resolved:
        root_id = resolved

    # Find delta files for this tree
    delta_parent = {}
    for df in all_deltas:
        sid = df.stem.replace(".delta", "")
        parent = find_parent_session(sid, trace_dir)
        if parent:
            delta_parent[sid] = parent

    def get_root(sid):
        visited = set()
        cur = sid
        while cur in delta_parent and cur not in visited:
            visited.add(cur)
            cur = delta_parent[cur]
        return cur

    tree_deltas = []
    for df in all_deltas:
        sid = df.stem.replace(".delta", "")
        if get_root(sid) == root_id or sid == root_id:
            tree_deltas.append((sid, df))

    tree_deltas.sort(key=lambda x: x[1].stat().st_mtime)

    # Analyze timing for each session
    timings = []
    for sid, df in tree_deltas:
        timing = analyze_delta_timing(df, sid)
        if timing:
            timings.append(timing)

    # Assign labels
    labels = assign_session_labels(timings, root_id)

    # === Section 1: Call Tree ===
    print(f"\n  Call tree (root: {root_id[:8]}...):\n")
    tree = build_tree(entries, trace_dir, root_id)
    if tree and tree["roots"]:
        for root in tree["roots"]:
            print_call_tree(tree, root, labels)
    else:
        print(f"  (no tree data for {root_id[:8]})")

    # === Section 2: Event Timeline ===
    all_events = []
    for sid, df in tree_deltas:
        all_events.extend(extract_events(df, sid))

    print(f"\n  Timeline:")
    print_event_timeline(all_events, labels, verbose=args.verbose)

    # === Section 3: Where the Time Goes ===
    print_where_time_goes(timings)

    # === Section 4: Context Growth ===
    print_context_growth(timings, labels)

    print()


if __name__ == "__main__":
    main()
