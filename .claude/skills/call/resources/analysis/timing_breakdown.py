#!/usr/bin/env python3
"""
timing_breakdown.py — Analyze where time is spent in a /call session tree.

Reads delta JSONL files to extract per-message timestamps, then categorizes
time into: model thinking, tool calls, fork overhead, relay, and idle.

Usage:
    python3 timing_breakdown.py <project_dir> --root <session_id>
    python3 timing_breakdown.py <project_dir> --last
    python3 timing_breakdown.py <project_dir> --last --verbose

Examples:
    python3 timing_breakdown.py ~/.claude/projects/-Users-amolk-work-call-agents-examples-customer-support --last
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class MessageInfo:
    """A parsed message from a session JSONL."""
    index: int
    msg_type: str
    timestamp: datetime | None
    content_preview: str
    tool_name: str | None = None
    is_fork_boundary: bool = False
    is_call_executed: bool = False
    is_task_prompt: bool = False
    raw_type: str = ""


@dataclass
class SessionTimeline:
    """Timing analysis for a single session."""
    session_id: str
    messages: list[MessageInfo] = field(default_factory=list)
    fork_start: datetime | None = None  # when fork boundary appears
    task_start: datetime | None = None  # when task prompt appears
    first_response: datetime | None = None  # first assistant message after task
    need_input_time: datetime | None = None  # when NEED_INPUT emitted
    result_time: datetime | None = None  # when RESULT emitted
    parent_session: str | None = None
    task: str = ""
    depth: int = 0
    file_size: int = 0

    @property
    def startup_time(self) -> float | None:
        """Time from fork boundary to first assistant response."""
        if self.fork_start and self.first_response:
            return (self.first_response - self.fork_start).total_seconds()
        return None

    @property
    def total_own_time(self) -> float | None:
        """Time from task prompt to final output (NEED_INPUT or RESULT)."""
        end = self.need_input_time or self.result_time
        if self.task_start and end:
            return (end - self.task_start).total_seconds()
        return None


@dataclass
class TimeSegment:
    """A categorized time segment."""
    category: str  # thinking, tool_call, fork_overhead, relay, skill_load
    start: datetime
    end: datetime
    detail: str = ""

    @property
    def duration(self) -> float:
        return (self.end - self.start).total_seconds()


def parse_timestamp(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        # Handle various formats
        ts = ts.rstrip("Z")
        if "." in ts:
            return datetime.fromisoformat(ts)
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def extract_content_info(obj: dict) -> tuple[str, str | None]:
    """Extract content preview and tool name from a message object."""
    content = ""
    tool_name = None
    if "message" not in obj or "content" not in obj["message"]:
        return "", None

    c = obj["message"]["content"]
    if isinstance(c, str):
        content = c[:200].replace("\n", " ")
    elif isinstance(c, list):
        for block in c:
            if not isinstance(block, dict):
                continue
            bt = block.get("type", "")
            if bt == "text" and not content:
                content = block.get("text", "")[:200].replace("\n", " ")
            elif bt == "tool_use":
                tool_name = block.get("name", "?")
                if not content:
                    content = f"[tool: {tool_name}]"
            elif bt == "tool_result":
                if not content:
                    content = "[tool_result]"
    return content, tool_name


def parse_delta(delta_file: Path) -> SessionTimeline:
    """Parse a delta JSONL file into a SessionTimeline."""
    session_id = delta_file.stem.replace(".delta", "")
    timeline = SessionTimeline(session_id=session_id)
    timeline.file_size = delta_file.stat().st_size

    post_fork = False
    task_received = False

    with open(delta_file) as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = obj.get("type", "?")
            ts = parse_timestamp(obj.get("timestamp", ""))
            content, tool_name = extract_content_info(obj)

            # Keep raw content (with newlines) for fork boundary parsing
            raw_content = ""
            if "message" in obj and "content" in obj["message"]:
                c = obj["message"]["content"]
                if isinstance(c, str):
                    raw_content = c

            msg = MessageInfo(
                index=i,
                msg_type=msg_type,
                timestamp=ts,
                content_preview=content[:150],
                tool_name=tool_name,
                raw_type=msg_type,
            )

            # Detect fork boundary — deltas inherit earlier fork boundaries
            # from parent sessions. Match on "Forked session:" to find this
            # session's own boundary; reset state only for that one.
            if "Forking session for /call" in raw_content:
                # Check if this boundary created THIS session
                forked_id = None
                parent_id = None
                depth_val = None
                for part in raw_content.split("\n"):
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

                is_own_fork = (
                    forked_id and forked_id.startswith(session_id[:8])
                ) if forked_id else True  # fallback: treat all as own (old format)

                if is_own_fork:
                    msg.is_fork_boundary = True
                    post_fork = True
                    task_received = False
                    timeline.fork_start = ts
                    timeline.task_start = None
                    timeline.first_response = None
                    timeline.need_input_time = None
                    timeline.result_time = None
                    timeline.messages.clear()
                    if parent_id:
                        timeline.parent_session = parent_id
                    if depth_val is not None:
                        timeline.depth = depth_val

            # Detect [/call executed: ...] marker
            if content.startswith("[/call executed:"):
                msg.is_call_executed = True

            # Detect task prompt (forked session system instruction)
            if "You are running in a forked session" in content and post_fork:
                msg.is_task_prompt = True
                task_received = True
                timeline.task_start = ts
                # Extract task from the content
                if "## Task" in content:
                    timeline.task = content.split("## Task")[-1].strip()[:200]

            # Detect first assistant response after task
            if (task_received and msg_type == "assistant" and ts
                    and timeline.first_response is None
                    and content and not content.startswith("No response")):
                timeline.first_response = ts

            # Detect NEED_INPUT
            if "---NEED_INPUT---" in content and msg_type == "assistant":
                timeline.need_input_time = ts

            # Detect RESULT
            if "---RESULT---" in content and msg_type == "assistant":
                timeline.result_time = ts

            timeline.messages.append(msg)

    return timeline


def categorize_time(timeline: SessionTimeline) -> list[TimeSegment]:
    """Break a session timeline into categorized time segments."""
    segments = []
    timed_msgs = [(m.timestamp, m) for m in timeline.messages if m.timestamp]
    if len(timed_msgs) < 2:
        return segments

    for i in range(len(timed_msgs) - 1):
        ts_start, msg = timed_msgs[i]
        ts_end, next_msg = timed_msgs[i + 1]
        duration = (ts_end - ts_start).total_seconds()

        if duration < 0.01:
            continue

        # Categorize based on what happened
        if msg.is_fork_boundary or msg.is_call_executed:
            category = "fork_overhead"
            detail = "session fork + startup"
        elif msg.tool_name == "Bash" and "callstack.py" in msg.content_preview:
            category = "child_call"
            detail = f"waiting for child forked session"
        elif msg.tool_name == "Bash":
            category = "tool_call"
            detail = f"Bash"
        elif msg.tool_name == "Skill":
            category = "skill_load"
            detail = f"loading skill"
        elif msg.tool_name == "ToolSearch":
            category = "tool_call"
            detail = "ToolSearch"
        elif msg.tool_name and msg.tool_name.startswith("mcp__"):
            category = "tool_call"
            short = msg.tool_name.split("__")[-1]
            detail = f"MCP: {short}"
        elif msg.tool_name == "Read":
            category = "tool_call"
            detail = "Read"
        elif msg.tool_name:
            category = "tool_call"
            detail = msg.tool_name
        elif msg.msg_type == "assistant":
            category = "thinking"
            detail = msg.content_preview[:60]
        elif msg.msg_type == "user" and msg.is_task_prompt:
            category = "fork_overhead"
            detail = "task injection"
        else:
            category = "other"
            detail = f"{msg.msg_type}: {msg.content_preview[:40]}"

        segments.append(TimeSegment(
            category=category,
            start=ts_start,
            end=ts_end,
            detail=detail,
        ))

    return segments


def format_duration(seconds: float) -> str:
    if seconds < 0.1:
        return f"{seconds*1000:.0f}ms"
    if seconds < 1:
        return f"{seconds:.2f}s"
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(seconds, 60)
    return f"{int(m)}m{s:.0f}s"


def find_deltas_for_root(root_id: str, trace_dir: Path) -> list[Path]:
    """Find all delta files belonging to a call tree rooted at root_id."""
    # Start with delta files and trace entries
    trace_file = trace_dir / "call_trace.jsonl"
    if not trace_file.exists():
        return []

    # Load all traces
    entries = []
    with open(trace_file) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))

    # Find all sessions in the tree by walking parent links
    all_deltas = list(trace_dir.glob("*.delta.jsonl"))
    session_parent = {}
    for df in all_deltas:
        sid = df.stem.replace(".delta", "")
        tl = parse_delta(df)
        if tl.parent_session:
            session_parent[sid] = tl.parent_session

    # Find sessions whose root is root_id
    def get_root(sid: str) -> str:
        visited = set()
        current = sid
        while current in session_parent and current not in visited:
            visited.add(current)
            current = session_parent[current]
        return current

    matching = []
    for df in all_deltas:
        sid = df.stem.replace(".delta", "")
        if get_root(sid) == root_id or sid == root_id:
            matching.append(df)

    # Sort by depth (approximate: by file content order)
    return sorted(matching, key=lambda f: f.stat().st_mtime)


def get_latest_root(trace_dir: Path) -> str | None:
    """Find the root of the most recent call tree."""
    trace_file = trace_dir / "call_trace.jsonl"
    if not trace_file.exists():
        return None

    entries = []
    with open(trace_file) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))

    if not entries:
        return None

    # Get most recent depth-1 entry
    depth1 = [e for e in entries if e.get("call_depth") == 1]
    if not depth1:
        return None

    latest = max(depth1, key=lambda e: e["timestamp"])
    sid = latest["session_id"]

    # Find its parent from delta
    delta_file = trace_dir / f"{sid}.delta.jsonl"
    if delta_file.exists():
        tl = parse_delta(delta_file)
        if tl.parent_session:
            return tl.parent_session

    return sid


def print_session_breakdown(timeline: SessionTimeline, segments: list[TimeSegment],
                            verbose: bool = False):
    """Print timing breakdown for a single session."""
    sid = timeline.session_id[:8]
    depth_label = f"depth={timeline.depth}" if timeline.depth else "root"
    task_label = timeline.task[:70] if timeline.task else "(root session)"
    size_kb = timeline.file_size / 1024

    print(f"\n{'='*70}")
    print(f"  [{sid}] {depth_label}  ({size_kb:.0f} KB context)")
    print(f"  task: {task_label}")
    if timeline.startup_time is not None:
        print(f"  startup: {format_duration(timeline.startup_time)}")
    if timeline.total_own_time is not None:
        print(f"  own time: {format_duration(timeline.total_own_time)}")
    print(f"{'='*70}")

    if not segments:
        print("  (no timed segments)")
        return

    # Aggregate by category
    by_cat = {}
    for seg in segments:
        if seg.category not in by_cat:
            by_cat[seg.category] = 0.0
        by_cat[seg.category] += seg.duration

    total = sum(by_cat.values())

    # Print category summary
    print(f"\n  {'Category':<20s} {'Time':>8s} {'%':>6s}")
    print(f"  {'─'*20} {'─'*8} {'─'*6}")
    for cat in ["thinking", "tool_call", "skill_load", "fork_overhead", "child_call", "other"]:
        dur = by_cat.get(cat, 0)
        if dur > 0:
            pct = (dur / total * 100) if total > 0 else 0
            print(f"  {cat:<20s} {format_duration(dur):>8s} {pct:>5.1f}%")
    print(f"  {'─'*20} {'─'*8} {'─'*6}")
    print(f"  {'TOTAL':<20s} {format_duration(total):>8s}")

    # Verbose: show each segment
    if verbose:
        print(f"\n  {'Time':>8s} {'Category':<15s} Detail")
        print(f"  {'─'*8} {'─'*15} {'─'*40}")
        for seg in segments:
            if seg.duration < 0.05:
                continue
            print(f"  {format_duration(seg.duration):>8s} {seg.category:<15s} {seg.detail[:50]}")


def print_summary(timelines: list[SessionTimeline], all_segments: list[list[TimeSegment]]):
    """Print an overall summary across all sessions."""
    print(f"\n{'='*70}")
    print(f"  OVERALL SUMMARY")
    print(f"{'='*70}")

    total_by_cat = {}
    for segments in all_segments:
        for seg in segments:
            if seg.category not in total_by_cat:
                total_by_cat[seg.category] = 0.0
            total_by_cat[seg.category] += seg.duration

    grand_total = sum(total_by_cat.values())

    print(f"\n  Sessions: {len(timelines)}")
    total_context = sum(t.file_size for t in timelines) / 1024
    print(f"  Total context loaded: {total_context:.0f} KB")

    print(f"\n  {'Category':<20s} {'Time':>8s} {'%':>6s}")
    print(f"  {'─'*20} {'─'*8} {'─'*6}")
    for cat in ["thinking", "tool_call", "skill_load", "fork_overhead", "child_call", "other"]:
        dur = total_by_cat.get(cat, 0)
        if dur > 0:
            pct = (dur / grand_total * 100) if grand_total > 0 else 0
            print(f"  {cat:<20s} {format_duration(dur):>8s} {pct:>5.1f}%")
    print(f"  {'─'*20} {'─'*8} {'─'*6}")
    print(f"  {'TOTAL':<20s} {format_duration(grand_total):>8s}")

    # Context growth
    print(f"\n  Context growth per level:")
    for tl in sorted(timelines, key=lambda t: t.depth):
        kb = tl.file_size / 1024
        print(f"    depth {tl.depth}: {kb:.0f} KB  [{tl.session_id[:8]}]")


def main():
    parser = argparse.ArgumentParser(description="Analyze timing in /call session trees")
    parser.add_argument("project_dir", help="Path to the Claude project directory")
    parser.add_argument("--root", help="Root session ID (prefix match OK)")
    parser.add_argument("--last", action="store_true", help="Analyze the most recent call tree")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show per-segment detail")
    args = parser.parse_args()

    project_dir = Path(args.project_dir)
    trace_dir = project_dir / "call_traces"

    if not trace_dir.exists():
        print(f"No call_traces directory found at {trace_dir}", file=sys.stderr)
        sys.exit(1)

    root_id = args.root
    if args.last:
        root_id = get_latest_root(trace_dir)
        if root_id:
            print(f"Latest root: {root_id}")
        else:
            print("No traces found", file=sys.stderr)
            sys.exit(1)

    if not root_id:
        print("Specify --root <session_id> or --last", file=sys.stderr)
        sys.exit(1)

    # Partial match — check both delta file names and parent IDs
    if len(root_id) < 36:
        all_deltas = list(trace_dir.glob("*.delta.jsonl"))
        # First check delta file names
        for df in all_deltas:
            sid = df.stem.replace(".delta", "")
            if sid.startswith(root_id):
                root_id = sid
                break
        else:
            # Check parent IDs from fork boundaries
            for df in all_deltas:
                tl = parse_delta(df)
                if tl.parent_session and tl.parent_session.startswith(root_id):
                    root_id = tl.parent_session
                    break

    delta_files = find_deltas_for_root(root_id, trace_dir)
    if not delta_files:
        print(f"No delta files found for root {root_id}", file=sys.stderr)
        sys.exit(1)

    timelines = []
    all_segments = []
    for df in delta_files:
        tl = parse_delta(df)
        segments = categorize_time(tl)
        # Filter out child_call segments (those are counted in children)
        own_segments = [s for s in segments if s.category != "child_call"]
        timelines.append(tl)
        all_segments.append(own_segments)
        print_session_breakdown(tl, own_segments, verbose=args.verbose)

    if len(timelines) > 1:
        print_summary(timelines, all_segments)


if __name__ == "__main__":
    main()
