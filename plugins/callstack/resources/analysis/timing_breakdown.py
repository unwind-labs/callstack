#!/usr/bin/env python3
"""Per-session duration breakdown from a call_trace.jsonl file.

Usage:
    timing_breakdown.py [path/to/call_trace.jsonl]
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_callstack.analysis import SessionAnalyzer, format_duration  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("trace_file", help="Path to a call_trace.jsonl (<log_dir>/<invoke_id>/call_trace.jsonl)")
    args = p.parse_args()

    trace = Path(args.trace_file)
    if not trace.exists():
        sys.exit(f"trace file not found: {trace}")

    events = SessionAnalyzer().trace_events(trace)
    if not events:
        sys.exit("no events")

    by_sess: dict[str, float] = defaultdict(float)
    turns: dict[str, int] = defaultdict(int)
    errors: dict[str, int] = defaultdict(int)
    for e in events:
        by_sess[e.session_id] += e.duration
        turns[e.session_id] += 1
        if e.error:
            errors[e.session_id] += 1

    rows = sorted(by_sess.items(), key=lambda kv: -kv[1])
    total = sum(by_sess.values())
    print(f"{'session':12s}  {'turns':>6s}  {'errors':>7s}  {'duration':>10s}  pct")
    for sid, dur in rows:
        pct = 100.0 * dur / total if total else 0.0
        print(f"{sid[:12]:12s}  {turns[sid]:>6d}  {errors[sid]:>7d}  {format_duration(dur):>10s}  {pct:5.1f}%")
    print(f"{'TOTAL':12s}  {sum(turns.values()):>6d}  {sum(errors.values()):>7d}  {format_duration(total):>10s}")


if __name__ == "__main__":
    main()
