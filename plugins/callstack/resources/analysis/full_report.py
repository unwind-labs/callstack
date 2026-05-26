#!/usr/bin/env python3
"""Full report: tree + per-session breakdown for one callstack run.

Usage:
    full_report.py [path/to/call_trace.jsonl] [--root <session-prefix>]
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_callstack.analysis import (  # noqa: E402
    SessionAnalyzer,
    format_duration,
    format_tree,
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

    root = _resolve_prefix(events, args.root)
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
    dur: dict[str, float] = defaultdict(float)
    turns: dict[str, int] = defaultdict(int)
    errors: dict[str, int] = defaultdict(int)
    for e in events:
        dur[e.session_id] += e.duration
        turns[e.session_id] += 1
        if e.error:
            errors[e.session_id] += 1

    total = sum(dur.values())
    print(f"{'session':14s}  {'turns':>6s}  {'errors':>7s}  {'duration':>10s}  pct")
    for sid, d in sorted(dur.items(), key=lambda kv: -kv[1]):
        pct = 100.0 * d / total if total else 0.0
        print(f"{sid[:14]:14s}  {turns[sid]:>6d}  {errors[sid]:>7d}  {format_duration(d):>10s}  {pct:5.1f}%")
    print(f"{'TOTAL':14s}  {sum(turns.values()):>6d}  {sum(errors.values()):>7d}  {format_duration(total):>10s}")


def _resolve_prefix(events: list, prefix: str | None) -> str | None:
    if prefix is None:
        return None
    sids = {e.session_id for e in events}
    matches = [s for s in sids if s.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        sys.exit(f"no session id starting with {prefix!r}")
    sys.exit(f"ambiguous prefix {prefix!r}: {sorted(matches)}")


if __name__ == "__main__":
    main()
