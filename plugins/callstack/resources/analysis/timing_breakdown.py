#!/usr/bin/env python3
"""Per-session duration breakdown from a call_trace.jsonl file.

Usage:
    timing_breakdown.py [path/to/call_trace.jsonl]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_callstack.analysis import (  # noqa: E402
    SessionAnalyzer,
    format_timing_table,
    per_session_timing,
)


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

    print(format_timing_table(per_session_timing(events), id_width=12))


if __name__ == "__main__":
    main()
