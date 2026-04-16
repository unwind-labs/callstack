#!/usr/bin/env python3
"""
session_inspect.py — Inspect the message-level timeline of a single session.

Shows every message with timestamps, types, and content previews. Useful for
understanding exactly what a forked session did and in what order.

Usage:
    python3 session_inspect.py <project_dir> <session_id>
    python3 session_inspect.py <project_dir> <session_id> --post-fork
    python3 session_inspect.py <project_dir> <session_id> --tools-only

Examples:
    python3 session_inspect.py ~/.claude/projects/-Users-amolk-work-call-agents-examples-customer-support a848ba56
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


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


def find_session_file(project_dir: Path, session_prefix: str) -> Path | None:
    """Find a session or delta file matching the prefix."""
    trace_dir = project_dir / "call_traces"

    # Check delta files first (they survive cleanup)
    if trace_dir.exists():
        for f in trace_dir.glob("*.delta.jsonl"):
            if f.stem.replace(".delta", "").startswith(session_prefix):
                return f

    # Check main session files
    for f in project_dir.glob("*.jsonl"):
        if f.stem.startswith(session_prefix):
            return f

    return None


def inspect_session(session_file: Path, post_fork_only: bool = False,
                    tools_only: bool = False):
    """Parse and display a session's messages."""
    print(f"File: {session_file}")
    print(f"Size: {session_file.stat().st_size / 1024:.1f} KB")
    print()

    messages = []
    with open(session_file) as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                messages.append((i, json.loads(line)))
            except json.JSONDecodeError:
                continue

    print(f"Total messages: {len(messages)}")

    fork_seen = False
    prev_ts = None

    print(f"\n{'#':>4s} {'Delta':>7s} {'Type':<20s} {'Content'}")
    print(f"{'─'*4} {'─'*7} {'─'*20} {'─'*50}")

    for idx, (line_no, obj) in enumerate(messages):
        msg_type = obj.get("type", "?")
        ts = parse_timestamp(obj.get("timestamp", ""))

        # Extract content
        content = ""
        tool_name = None
        if "message" in obj and "content" in obj["message"]:
            c = obj["message"]["content"]
            if isinstance(c, str):
                content = c[:200].replace("\n", " ")
            elif isinstance(c, list):
                parts = []
                for block in c:
                    if not isinstance(block, dict):
                        continue
                    bt = block.get("type", "")
                    if bt == "text":
                        parts.append(block.get("text", "")[:100].replace("\n", " "))
                    elif bt == "tool_use":
                        tool_name = block.get("name", "?")
                        parts.append(f"[tool: {tool_name}]")
                    elif bt == "tool_result":
                        result_text = ""
                        rc = block.get("content", "")
                        if isinstance(rc, str):
                            result_text = rc[:60].replace("\n", " ")
                        elif isinstance(rc, list):
                            for rb in rc:
                                if isinstance(rb, dict) and rb.get("type") == "text":
                                    result_text = rb.get("text", "")[:60].replace("\n", " ")
                                    break
                        parts.append(f"[result: {result_text}]")
                content = " | ".join(parts)

        # Track fork boundary
        if "Forking session for /call" in content or "FORK BOUNDARY" in content:
            fork_seen = True

        if post_fork_only and not fork_seen:
            continue

        if tools_only and not tool_name:
            continue

        # Compute delta from previous timestamp
        delta_str = ""
        if ts and prev_ts:
            delta = (ts - prev_ts).total_seconds()
            if delta >= 0.01:
                delta_str = f"+{format_duration(delta)}"

        if ts:
            prev_ts = ts

        # Truncate content for display
        display_content = content[:90]
        if len(content) > 90:
            display_content += "..."

        # Highlight special messages
        marker = ""
        if "FORK BOUNDARY" in content or "Forking session for /call" in content:
            marker = " ◆ FORK"
        elif "NEED_INPUT" in content:
            marker = " ◆ NEED_INPUT"
        elif "RESULT" in content and msg_type == "assistant":
            marker = " ◆ RESULT"
        elif "/call executed" in content:
            marker = " ◆ CALL_EXEC"
        elif "forked session" in content and "You are" in content:
            marker = " ◆ TASK"

        print(f"{idx:4d} {delta_str:>7s} {msg_type:<20s} {display_content}{marker}")


def main():
    parser = argparse.ArgumentParser(description="Inspect a single session's message timeline")
    parser.add_argument("project_dir", help="Path to the Claude project directory")
    parser.add_argument("session_id", help="Session ID (prefix match OK)")
    parser.add_argument("--post-fork", action="store_true",
                        help="Only show messages after the fork boundary")
    parser.add_argument("--tools-only", action="store_true",
                        help="Only show tool use messages")
    args = parser.parse_args()

    project_dir = Path(args.project_dir)
    session_file = find_session_file(project_dir, args.session_id)

    if not session_file:
        print(f"No session file found matching '{args.session_id}' in {project_dir}",
              file=sys.stderr)
        sys.exit(1)

    inspect_session(session_file, args.post_fork, args.tools_only)


if __name__ == "__main__":
    main()
