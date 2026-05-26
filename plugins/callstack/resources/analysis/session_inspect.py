#!/usr/bin/env python3
"""Print summary stats for one session JSONL file.

Usage:
    session_inspect.py <path/to/session.jsonl>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_callstack.analysis import SessionAnalyzer, format_duration  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("session_file", help="Path to a session JSONL file")
    args = p.parse_args()

    f = Path(args.session_file)
    if not f.exists():
        sys.exit(f"session file not found: {f}")

    stats = SessionAnalyzer().session_stats(f)
    print(f"Session: {f.name}")
    print(f"  Messages: {stats.message_count}")
    print(f"  Duration: {format_duration(stats.duration)}")
    if stats.first_timestamp:
        print(f"  First:    {stats.first_timestamp.isoformat()}")
    if stats.last_timestamp:
        print(f"  Last:     {stats.last_timestamp.isoformat()}")
    print("  By type:")
    for kind, n in sorted(stats.by_type.items(), key=lambda kv: -kv[1]):
        print(f"    {kind:20s} {n}")


if __name__ == "__main__":
    main()
