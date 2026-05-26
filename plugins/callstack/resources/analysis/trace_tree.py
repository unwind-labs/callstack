#!/usr/bin/env python3
"""Print the call tree from a call_trace.jsonl file.

Usage:
    trace_tree.py [path/to/call_trace.jsonl] [--root <session-prefix>]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_callstack.analysis import SessionAnalyzer, format_tree  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("trace_file", help="Path to a call_trace.jsonl (<log_dir>/<invoke_id>/call_trace.jsonl)")
    p.add_argument("--root", help="Root session id (or prefix) to render")
    args = p.parse_args()

    trace = Path(args.trace_file)
    if not trace.exists():
        sys.exit(f"trace file not found: {trace}")

    analyzer = SessionAnalyzer()
    root_id = _resolve_prefix(analyzer, trace, args.root)
    tree = analyzer.build_tree(trace, root_session=root_id)
    if tree is None:
        sys.exit("no events in trace; nothing to render")
    print(format_tree(tree))


def _resolve_prefix(analyzer: SessionAnalyzer, trace_file: Path, prefix: str | None) -> str | None:
    if prefix is None:
        return None
    sids = {e.session_id for e in analyzer.trace_events(trace_file)}
    matches = [s for s in sids if s.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        sys.exit(f"no session id starting with {prefix!r}")
    sys.exit(f"ambiguous prefix {prefix!r}: {sorted(matches)}")


if __name__ == "__main__":
    main()
