#!/usr/bin/env python3
"""
trace_tree.py — Reconstruct and display the /call tree from call_trace.jsonl.

Shows the hierarchy of forked sessions with timing, task, and status at each level.

Usage:
    python3 trace_tree.py <project_dir>
    python3 trace_tree.py <project_dir> --root <session_id>
    python3 trace_tree.py <project_dir> --last

Examples:
    python3 trace_tree.py ~/.claude/projects/-Users-amolk-work-call-agents-examples-customer-support
    python3 trace_tree.py ~/.claude/projects/-Users-amolk-work-call-agents-examples-customer-support --root fff6800e
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


def load_traces(trace_file: Path) -> list[dict]:
    """Load all trace entries from call_trace.jsonl."""
    entries = []
    with open(trace_file) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def group_by_session(entries: list[dict]) -> dict[str, list[dict]]:
    """Group trace entries by session_id, preserving order."""
    groups = defaultdict(list)
    for e in entries:
        groups[e["session_id"]].append(e)
    return groups


def find_parent_session(session_id: str, delta_dir: Path) -> str | None:
    """Read the delta file for a session and extract its parent session ID.

    Deltas inherit fork boundaries from all ancestor sessions. We find the
    correct one by matching 'Forked session: <session_id>' — the boundary
    that created THIS session — and reading its 'Parent session:' line.
    Falls back to the last fork boundary if no Forked session match is found
    (for sessions created before that field was added).
    """
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
                content = ""
                if "message" in obj and "content" in obj["message"]:
                    c = obj["message"]["content"]
                    if isinstance(c, str):
                        content = c
                if "Forking session for /call" in content and "Parent session:" in content:
                    parent_in_block = None
                    forked_id_in_block = None
                    for part in content.split("\n"):
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


def find_root_from_trace(session_id: str, delta_dir: Path, all_sessions: set[str]) -> str:
    """Walk parent chain to find the root session."""
    visited = set()
    current = session_id
    while current and current not in visited:
        visited.add(current)
        parent = find_parent_session(current, delta_dir)
        if parent is None or parent not in all_sessions:
            # Check if parent itself has a delta (it might be the root which has no delta)
            return current
        current = parent
    return current


def build_tree(entries: list[dict], delta_dir: Path, root_session: str | None = None) -> dict:
    """Build a tree of session relationships from trace entries and delta files."""
    by_session = group_by_session(entries)
    all_sessions = set(by_session.keys())

    # Find parent for each session
    parent_map = {}
    children_map = defaultdict(list)
    for sid in all_sessions:
        parent = find_parent_session(sid, delta_dir)
        if parent:
            parent_map[sid] = parent
            children_map[parent].append(sid)

    # Find roots (sessions with no parent in our set)
    roots = [sid for sid in all_sessions if sid not in parent_map]

    if root_session:
        # Resolve prefix match against all known IDs (traced + parents)
        all_known = set(all_sessions) | set(children_map.keys())
        resolved = None
        for sid in all_known:
            if sid == root_session or sid.startswith(root_session):
                resolved = sid
                break
        if resolved:
            root_session = resolved
            roots = [root_session]
        else:
            print(f"Warning: session {root_session} not found in traces", file=sys.stderr)
            return {}

    return {
        "by_session": by_session,
        "parent_map": parent_map,
        "children_map": children_map,
        "roots": roots,
    }


def format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds*1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(seconds, 60)
    return f"{int(m)}m{s:.0f}s"


def print_tree(tree: dict, node: str, indent: int = 0):
    """Recursively print the call tree."""
    by_session = tree["by_session"]
    children_map = tree["children_map"]

    entries = by_session.get(node, [])
    prefix = "  " * indent + ("├─ " if indent > 0 else "")

    if entries:
        first = entries[0]
        task = first.get("task", "?")
        if len(task) > 80:
            task = task[:77] + "..."
        depth = first.get("call_depth", "?")
        total_dur = sum(e["duration_seconds"] for e in entries)
        phases = []
        for e in entries:
            t = e.get("task", "")
            label = "resumed" if t == "(resumed)" else "initial"
            phases.append(f"{label}={format_duration(e['duration_seconds'])}")
        error = any(e.get("error") for e in entries)

        sid_short = node[:8]
        status = "ERR" if error else "OK"
        print(f"{prefix}[{sid_short}] depth={depth} {format_duration(total_dur)} ({', '.join(phases)}) [{status}]")
        print(f"{prefix}  task: {task}")
    else:
        sid_short = node[:8]
        print(f"{prefix}[{sid_short}] (root — no trace entry)")

    children = children_map.get(node, [])
    # Sort children by timestamp
    def sort_key(sid):
        es = by_session.get(sid, [])
        return es[0]["timestamp"] if es else ""
    children.sort(key=sort_key)

    for child in children:
        print_tree(tree, child, indent + 1)


def get_latest_root(entries: list[dict], delta_dir: Path) -> str | None:
    """Find the root session of the most recent trace entry."""
    if not entries:
        return None
    by_session = group_by_session(entries)
    all_sessions = set(by_session.keys())

    # Get the most recent entry
    latest = max(entries, key=lambda e: e["timestamp"])
    latest_sid = latest["session_id"]

    # Walk up to root
    current = latest_sid
    visited = set()
    while current and current not in visited:
        visited.add(current)
        parent = find_parent_session(current, delta_dir)
        if parent is None:
            return current
        current = parent
    return current


def main():
    parser = argparse.ArgumentParser(description="Display /forked session tree from trace data")
    parser.add_argument("project_dir", help="Path to the Claude project directory")
    parser.add_argument("--root", help="Root session ID (prefix match OK)")
    parser.add_argument("--last", action="store_true", help="Show tree for the most recent call")
    parser.add_argument("--all", action="store_true", help="Show all trees")
    args = parser.parse_args()

    project_dir = Path(args.project_dir)
    trace_dir = project_dir / "call_traces"
    trace_file = trace_dir / "call_trace.jsonl"

    if not trace_file.exists():
        print(f"No trace file found at {trace_file}", file=sys.stderr)
        sys.exit(1)

    entries = load_traces(trace_file)
    if not entries:
        print("No trace entries found", file=sys.stderr)
        sys.exit(1)

    root_session = args.root
    if args.last:
        root_session = get_latest_root(entries, trace_dir)
        if root_session:
            print(f"Latest root: {root_session}\n")

    tree = build_tree(entries, trace_dir, root_session)
    if not tree:
        sys.exit(1)

    roots = tree["roots"]
    if not roots:
        # The root itself may not be in traces — find depth-1 entries
        depth1 = [e for e in entries if e.get("call_depth") == 1]
        if depth1:
            # Use the parent from the first depth-1 entry
            parent = find_parent_session(depth1[0]["session_id"], trace_dir)
            if parent:
                roots = [parent]
                tree["roots"] = roots

    for root in sorted(roots):
        print_tree(tree, root)
        print()


if __name__ == "__main__":
    main()
