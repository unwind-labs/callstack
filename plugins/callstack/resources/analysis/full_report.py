#!/usr/bin/env python3
"""Full report: tree + per-session breakdown for one callstack run.

Usage:
    full_report.py [path/to/call_trace.jsonl] [--root <session-prefix>]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_callstack.analysis import (  # noqa: E402
    SessionAnalyzer,
    SessionPrefixError,
    format_timing_table,
    format_tree,
    per_session_timing,
    resolve_session_prefix,
)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("trace_file", help="Path to a call_trace.jsonl (<log_dir>/<invoke_id>/call_trace.jsonl)")
    p.add_argument("--root", help="Root session id (or prefix)")
    args = p.parse_args()

    trace = Path(args.trace_file)
    if not trace.exists():
        sys.exit(f"trace file not found: {trace}")

    analyzer = SessionAnalyzer()
    events = analyzer.trace_events(trace)
    if not events:
        sys.exit("no events in trace")

    try:
        root = resolve_session_prefix({e.session_id for e in events}, args.root)
    except SessionPrefixError as exc:
        sys.exit(str(exc))
    tree = analyzer.build_tree(trace, root_session=root)

    print("=" * 70)
    print("CALL TREE")
    print("=" * 70)
    if tree:
        print(format_tree(tree))
    print()

    print("=" * 70)
    print("PER-SESSION BREAKDOWN")
    print("=" * 70)
    print(format_timing_table(per_session_timing(events), id_width=14))


if __name__ == "__main__":
    main()
