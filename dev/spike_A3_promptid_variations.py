"""Spike A3 — try promptId / envelope-shape variations to revive a
dangling tool_use.

Spike A established that injecting a stream-json `user` message
containing a `tool_result` block whose `tool_use_id` matches the
dangling tool DOES NOT cause claude to resume the existing prompt
cycle. The transcript (Spike A2) showed the dangling line and its
prior user message share a single `promptId`, suggesting the API
server tracks the cycle by promptId — and a fresh user message via
stream-json mints a new one, abandoning the cycle.

This spike sets up one dangling parent, then runs multiple resume
variants against the SAME transcript:

  V0 (control) — same as Spike A: type=user + tool_result block, no
                 promptId. Expected: re-runs (FAIL).
  V1 — type=user + promptId=<original> + tool_result block.
       Hypothesis: matching the cycle's promptId continues it.
  V2 — type=tool_result (top-level envelope). Hypothesis: a different
       wire-format that bypasses the user-message → new-cycle path.
  V3 — type=user + parent_tool_use_id=<dangling id> + tool_result.
       Hypothesis: the CLI uses parent_tool_use_id to thread the
       message under the existing assistant turn.

For each: send the variant, drain stream-json for up to 30s, classify
output as one of:
  RECALL   — model re-emitted the dangling tool's name as a fresh
             tool_use (= variant didn't work; same as Spike A).
  CONTINUE — model produced a non-tool-use assistant text after the
             injection (= variant worked).
  EMPTY    — no output (claude rejected the input silently).
  ERROR    — stderr/result indicates a protocol error.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import uuid
from pathlib import Path


TOOL_NAME = "mcp__blocker__block_then_ok"


def _write_blocker(tmpdir: Path) -> Path:
    server = tmpdir / "blocker_mcp.py"
    server.write_text(textwrap.dedent(f"""
        from mcp.server.fastmcp import FastMCP
        import time
        from pathlib import Path
        mcp = FastMCP("blocker")
        @mcp.tool()
        def block_then_ok(message: str = "hi") -> str:
            Path("{tmpdir}/sentinel").write_text(message)
            while True:
                time.sleep(1)
        if __name__ == "__main__":
            mcp.run()
    """).lstrip())
    return server


def _mcp_cfg(tmpdir: Path, server: Path) -> Path:
    cfg = tmpdir / ".mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"blocker": {"command": sys.executable, "args": [str(server)]}}}))
    return cfg


def _clean_env() -> dict:
    return {k: v for k, v in os.environ.items()
            if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                         "CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH",
                         "CLAUDE_CODE_ENTRYPOINT")}


def _send(stdin, obj: dict) -> None:
    stdin.write(json.dumps(obj) + "\n")
    stdin.flush()


def _handshake(stdin) -> None:
    _send(stdin, {
        "type": "control_request",
        "request_id": f"req_init_{uuid.uuid4().hex[:8]}",
        "request": {"subtype": "initialize", "hooks": None},
    })


def _claude_args(*, mcp_cfg: Path | None = None, resume: str | None = None) -> list[str]:
    a = ["claude", "--output-format", "stream-json", "--input-format", "stream-json",
         "--verbose", "--permission-prompt-tool", "stdio", "--permission-mode", "bypassPermissions"]
    if mcp_cfg is not None:
        a.extend(["--mcp-config", str(mcp_cfg)])
    if resume is not None:
        a.extend(["--resume", resume])
    return a


def _setup_dangling(tmp: Path, env: dict, cfg: Path) -> tuple[str, str, str]:
    """Spawn a parent claude that calls the blocker and SIGKILL it.
    Returns (session_id, dangling_tool_use_id, original_prompt_id)."""
    prompt = f"Call the {TOOL_NAME} tool with message='spike-A3'. Use it immediately. Don't say anything else."
    proc = subprocess.Popen(
        _claude_args(mcp_cfg=cfg), cwd=str(tmp),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True, env=env,
    )
    assert proc.stdin and proc.stdout
    _handshake(proc.stdin)
    _send(proc.stdin, {"type": "user", "session_id": "", "message": {"role": "user", "content": prompt}, "parent_tool_use_id": None})
    sid_holder: dict = {}
    def drain():
        assert proc.stdout
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            s = msg.get("session_id")
            if s and "id" not in sid_holder:
                sid_holder["id"] = s
            if msg.get("type") == "control_request" and msg.get("request", {}).get("subtype") == "can_use_tool":
                _send(proc.stdin, {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": msg.get("request_id", ""),
                        "response": {"behavior": "allow", "updatedInput": msg.get("request", {}).get("input", {})},
                    },
                })
    threading.Thread(target=drain, daemon=True).start()
    sentinel = tmp / "sentinel"
    t0 = time.monotonic()
    while not sentinel.exists() and time.monotonic() - t0 < 90:
        time.sleep(0.2)
    if not sentinel.exists():
        proc.kill()
        raise RuntimeError("sentinel never appeared")
    print("[setup] sentinel hit; sleeping 10s for transcript flush", flush=True)
    time.sleep(10)
    sid = sid_holder["id"]
    print(f"[setup] kill parent claude (sid={sid})", flush=True)
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass

    # Find transcript and extract dangling tool_use_id + its promptId.
    candidates = list(Path.home().glob(f".claude/projects/*/{sid}.jsonl"))
    if not candidates:
        raise RuntimeError(f"no transcript for {sid}")
    transcript = candidates[0]
    tool_uses: dict[str, str] = {}  # id -> promptId of grouping
    tool_results: set[str] = set()
    last_prompt_id = ""
    for line in transcript.read_text().splitlines():
        if not line.strip():
            continue
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("type") == "user" and m.get("promptId"):
            last_prompt_id = m["promptId"]
        msg = m.get("message", {})
        if msg.get("role") == "assistant":
            for b in msg.get("content", []) or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tool_uses[b["id"]] = last_prompt_id
        if msg.get("role") == "user":
            for b in msg.get("content", []) or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tool_results.add(b["tool_use_id"])
    pending = [tid for tid in tool_uses if tid not in tool_results]
    if not pending:
        raise RuntimeError(f"no dangling tool_use in transcript {transcript}")
    danging_tid = pending[-1]
    original_prompt_id = tool_uses[danging_tid]
    print(f"[setup] dangling tool_use_id={danging_tid} promptId={original_prompt_id}", flush=True)
    return sid, danging_tid, original_prompt_id


def _try_variant(label: str, sid: str, env: dict, cfg: Path, tmp: Path,
                 injection: dict, *, second_message: dict | None = None) -> str:
    """Resume the session and inject `injection`. Classify result."""
    print(f"\n========== {label} ==========", flush=True)
    print(f"  injection: {json.dumps(injection)[:300]}", flush=True)
    rproc = subprocess.Popen(
        _claude_args(mcp_cfg=cfg, resume=sid), cwd=str(tmp),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, start_new_session=True,
    )
    assert rproc.stdin and rproc.stdout
    _handshake(rproc.stdin)
    _send(rproc.stdin, injection)
    if second_message is not None:
        # Some variants need a follow-up to nudge progress.
        time.sleep(1)
        _send(rproc.stdin, second_message)
    rproc.stdin.close()

    saw_tool_use_for_dangling = False
    saw_assistant_text = False
    saw_result = False
    error_msg = ""
    out_lines: list[dict] = []
    def drain():
        nonlocal saw_tool_use_for_dangling, saw_assistant_text, saw_result, error_msg
        assert rproc.stdout
        for line in rproc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            out_lines.append(msg)
            mtype = msg.get("type")
            if mtype == "assistant":
                for b in msg.get("message", {}).get("content", []) or []:
                    if isinstance(b, dict):
                        if b.get("type") == "tool_use" and b.get("name") == TOOL_NAME:
                            saw_tool_use_for_dangling = True
                        if b.get("type") == "text" and b.get("text"):
                            saw_assistant_text = True
            if mtype == "result":
                saw_result = True
                if msg.get("is_error"):
                    error_msg = msg.get("result", "")[:200]
            if mtype == "control_request":
                req = msg.get("request", {})
                if req.get("subtype") == "can_use_tool":
                    # Deny to avoid actually blocking again.
                    _send_safe(rproc, {
                        "type": "control_response",
                        "response": {"subtype": "success", "request_id": msg.get("request_id", ""),
                                     "response": {"behavior": "deny", "message": "spike A3 intercepts"}},
                    })
    t = threading.Thread(target=drain, daemon=True)
    t.start()
    try:
        rproc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        rproc.kill()
    t.join(timeout=5)

    # Show first/last few messages.
    print(f"  output ({len(out_lines)} msgs); types: {[m.get('type') for m in out_lines]}")
    for m in out_lines:
        mt = m.get("type")
        if mt == "assistant":
            content = m.get("message", {}).get("content", []) or []
            for b in content:
                if isinstance(b, dict):
                    if b.get("type") == "tool_use":
                        print(f"    assistant tool_use: name={b.get('name')} id={b.get('id')}")
                    elif b.get("type") == "text":
                        print(f"    assistant text: {b.get('text', '')[:200]}")
        elif mt == "result":
            print(f"    result: is_error={m.get('is_error')} result={str(m.get('result',''))[:200]}")
    if error_msg:
        verdict = "ERROR"
    elif saw_tool_use_for_dangling:
        verdict = "RECALL"
    elif saw_assistant_text and saw_result:
        verdict = "CONTINUE"
    elif not out_lines or (len(out_lines) <= 3 and not saw_result):
        verdict = "EMPTY"
    else:
        verdict = f"OTHER (text={saw_assistant_text} result={saw_result})"
    print(f"  VERDICT[{label}]: {verdict}", flush=True)
    return verdict


def _send_safe(proc, obj: dict) -> None:
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()
    except (OSError, ValueError):
        pass


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="spike-A3-") as raw:
        tmp = Path(raw)
        env = _clean_env()
        cfg = _mcp_cfg(tmp, _write_blocker(tmp))

        sid, dangling_tid, original_prompt_id = _setup_dangling(tmp, env, cfg)

        # Variant 0 (control): same shape as Spike A.
        v0 = {
            "type": "user", "session_id": "",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": dangling_tid, "content": "spike-A3 V0"}
            ]},
            "parent_tool_use_id": None,
        }

        # Variant 1: explicit promptId matching the dangling cycle.
        v1 = dict(v0)
        v1["promptId"] = original_prompt_id
        v1["message"] = {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": dangling_tid, "content": "spike-A3 V1"}
        ]}

        # Variant 2: top-level tool_result envelope (alternate wire shape).
        v2 = {
            "type": "tool_result",
            "tool_use_id": dangling_tid,
            "content": "spike-A3 V2",
        }
        # Fallback nudge if claude ignores V2 entirely.
        v2_nudge = {
            "type": "user", "session_id": "",
            "message": {"role": "user", "content": "Please continue."},
            "parent_tool_use_id": None,
        }

        # Variant 3: user message with parent_tool_use_id set to the dangling id.
        v3 = {
            "type": "user", "session_id": "",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": dangling_tid, "content": "spike-A3 V3"}
            ]},
            "parent_tool_use_id": dangling_tid,
        }

        results = {
            "V0 control": _try_variant("V0 control", sid, env, cfg, tmp, v0),
            "V1 promptId": _try_variant("V1 promptId", sid, env, cfg, tmp, v1),
            "V2 top-level": _try_variant("V2 top-level", sid, env, cfg, tmp, v2, second_message=v2_nudge),
            "V3 parent_tool_use_id": _try_variant("V3 parent_tool_use_id", sid, env, cfg, tmp, v3),
        }

        print("\n========== SUMMARY ==========")
        for name, verdict in results.items():
            print(f"  {name:30s} → {verdict}")
        # Any CONTINUE means we have a winner.
        winners = [k for k, v in results.items() if v == "CONTINUE"]
        print(f"\nWinners: {winners or 'NONE — registry short-circuit is the path'}")
        return 0 if winners else 1


if __name__ == "__main__":
    raise SystemExit(main())
