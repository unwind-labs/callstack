"""Spike A — does `claude --resume` + stream-json tool_result injection
work?

This is the load-bearing assumption of RFC-harvest-on-demand §"Re-spawn
+ inject path." If it doesn't work, the RFC's harvest mechanism cannot
revive a parent that was killed mid-`/call` — the parent's transcript
has a dangling `mcp__plugin_callstack_call__call` tool_use, and we need
a way to feed it the bundled child results so it continues.

The spike:

  1. Spin up a minimal stdio MCP server with one tool: `block_then_ok`,
     which writes a sentinel file and then sleeps forever.
  2. Run `claude -p` with the prompt "Call the block_then_ok tool",
     capturing stream-json output.
  3. Wait for the sentinel — confirms claude has issued the tool_use
     (the MCP server is now blocked on its sleep).
  4. SIGKILL the claude process. Its transcript on disk now has a
     dangling tool_use.
  5. Restart claude via `claude --resume <id> --input-format stream-json
     --output-format stream-json` with NO mcp config (so it can't
     re-call the tool itself).
  6. Inject a single stream-json line:
       {"type":"user","session_id":"...","message":{"role":"user",
        "content":[{"type":"tool_result","tool_use_id":"<id>",
                    "content":"injected by spike"}]},
        "parent_tool_use_id":null}
  7. Close stdin, read output. Pass if claude emits an assistant
     `result` message acknowledging the injected result. Fail if
     claude errors, exits, or hangs.

Run:
  PYTHONPATH=plugins/callstack python dev/spike_A_tool_result_inject.py

Notes:
  - Needs the `claude` CLI on PATH.
  - Uses a tmp project dir to avoid polluting real transcripts.
  - The spike's pass/fail is printed at the end. Read it.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path


def _write_mcp_server(tmpdir: Path) -> Path:
    """Write a minimal stdio MCP server with one blocking tool.

    Uses FastMCP (already a callstack dep) to keep the boilerplate
    short. The tool writes a sentinel at `<tmpdir>/sentinel` so we
    can synchronize on it before SIGKILL."""
    server = tmpdir / "blocker_mcp.py"
    server.write_text(textwrap.dedent(f"""
        from mcp.server.fastmcp import FastMCP
        import time
        from pathlib import Path

        mcp = FastMCP("blocker")

        @mcp.tool()
        def block_then_ok(message: str = "hi") -> str:
            \"\"\"Writes a sentinel file then sleeps forever. The spike
            kills the parent claude while we're sleeping.\"\"\"
            Path("{tmpdir}/sentinel").write_text(message)
            while True:
                time.sleep(1)

        if __name__ == "__main__":
            mcp.run()
    """).lstrip())
    return server


def _write_mcp_config(tmpdir: Path, server_path: Path) -> Path:
    cfg = tmpdir / ".mcp.json"
    cfg.write_text(json.dumps({
        "mcpServers": {
            "blocker": {
                "command": sys.executable,
                "args": [str(server_path)],
            }
        }
    }, indent=2))
    return cfg


def _wait_for_sentinel(sentinel: Path, timeout: float = 60.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if sentinel.exists():
            return True
        time.sleep(0.2)
    return False


def _read_stream_json_lines(proc: subprocess.Popen, max_lines: int = 200, label: str = "") -> list[dict]:
    """Drain stream-json output until process exits or max_lines hit.

    Prints each parsed message live so a hang is visible in tail."""
    lines: list[dict] = []
    if proc.stdout is None:
        return lines
    while len(lines) < max_lines:
        raw = proc.stdout.readline()
        if not raw:
            break
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
            lines.append(msg)
            print(f"[spike{' '+label if label else ''}] ← {msg.get('type','?')}: {json.dumps(msg)[:250]}", flush=True)
        except json.JSONDecodeError:
            lines.append({"_parse_error": raw[:200]})
            print(f"[spike{' '+label if label else ''}] ← unparseable: {raw[:200]}", flush=True)
    return lines


def _find_pending_tool_use(transcript: Path) -> tuple[str, str] | None:
    """Scan the session transcript JSONL for the last assistant
    tool_use that has no matching tool_result. Returns
    (session_id, tool_use_id) or None."""
    tool_uses: dict[str, str] = {}  # tool_use_id -> session_id
    tool_results: set[str] = set()
    for line in transcript.read_text().splitlines():
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = msg.get("sessionId") or msg.get("session_id") or ""
        # Assistant tool_use blocks.
        m = msg.get("message", {})
        if m.get("role") == "assistant":
            for block in m.get("content", []) or []:
                if block.get("type") == "tool_use":
                    tool_uses[block["id"]] = sid
        # User tool_result blocks.
        if m.get("role") == "user":
            for block in m.get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_results.add(block["tool_use_id"])
    pending = [tid for tid in tool_uses if tid not in tool_results]
    if not pending:
        return None
    last = pending[-1]
    return (tool_uses[last], last)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="spike-A-") as raw_tmp:
        tmpdir = Path(raw_tmp)
        sentinel = tmpdir / "sentinel"
        server = _write_mcp_server(tmpdir)
        cfg = _write_mcp_config(tmpdir, server)

        # ----- Step 1-3: spawn claude, get it to call the blocker tool -----
        print(f"[spike] tmpdir: {tmpdir}", flush=True)
        prompt = (
            "Call the mcp__blocker__block_then_ok tool with message='spike'. "
            "Use it immediately. Don't say anything else."
        )
        # Follow channel.py's protocol: no -p; send the user message via
        # stream-json on stdin after an init handshake. `--permission-
        # prompt-tool stdio` is what makes the permission decisions flow
        # over stdin in stream-json mode.
        claude_args = [
            "claude",
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--verbose",
            "--permission-prompt-tool", "stdio",
            "--permission-mode", "bypassPermissions",
            "--mcp-config", str(cfg),
        ]
        print(f"[spike] $ {' '.join(claude_args)}", flush=True)
        # Auth: prefer the saved OAuth in ~/.claude.json over the
        # ephemeral ANTHROPIC_API_KEY this process inherited from a
        # parent Claude Code SDK session — that key is bound to the
        # parent's session and the standalone `claude` CLI rejects it
        # with 401. Stripping the env-overrides lets claude fall back
        # to ~/.claude.json.
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                                  "CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH",
                                  "CLAUDE_CODE_ENTRYPOINT")}
        proc = subprocess.Popen(
            claude_args,
            cwd=str(tmpdir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=clean_env,
        )
        assert proc.stdin is not None

        # 1) Init handshake (matches channel.py).
        import uuid
        proc.stdin.write(json.dumps({
            "type": "control_request",
            "request_id": f"req_init_{uuid.uuid4().hex[:8]}",
            "request": {"subtype": "initialize", "hooks": None},
        }) + "\n")
        # 2) User message carrying the prompt.
        proc.stdin.write(json.dumps({
            "type": "user",
            "session_id": "",
            "message": {"role": "user", "content": prompt},
            "parent_tool_use_id": None,
        }) + "\n")
        proc.stdin.flush()

        # Read stream-json output in a background thread until sentinel.
        early_session_id: dict[str, str] = {}
        seen_msgs: list[dict] = []
        import threading
        stdin_lock = threading.Lock()
        def _drain_stdout():
            if proc.stdout is None:
                return
            while True:
                raw = proc.stdout.readline()
                if not raw:
                    return
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    print(f"[spike] unparseable: {raw[:200]}", flush=True)
                    continue
                seen_msgs.append(msg)
                mtype = msg.get("type")
                print(f"[spike] ← {mtype}: {json.dumps(msg)[:250]}", flush=True)
                sid = msg.get("session_id")
                if sid and "id" not in early_session_id:
                    early_session_id["id"] = sid
                    print(f"[spike] captured session_id={sid}", flush=True)
                # Answer can_use_tool with allow (we asked for bypass, but
                # if claude sends one anyway, allow it).
                if mtype == "control_request":
                    req = msg.get("request", {})
                    if req.get("subtype") == "can_use_tool":
                        resp = {
                            "type": "control_response",
                            "response": {
                                "subtype": "success",
                                "request_id": msg.get("request_id", ""),
                                "response": {"behavior": "allow", "updatedInput": req.get("input", {})},
                            },
                        }
                        with stdin_lock:
                            if proc.stdin and not proc.stdin.closed:
                                proc.stdin.write(json.dumps(resp) + "\n")
                                proc.stdin.flush()
        t = threading.Thread(target=_drain_stdout, daemon=True)
        t.start()

        print("[spike] waiting for sentinel (tool called)…", flush=True)
        ok = _wait_for_sentinel(sentinel, timeout=120.0)
        if not ok:
            print("[spike] FAIL: sentinel never appeared; claude didn't call the tool", flush=True)
            # Don't block on stderr.read() — process may still be alive.
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
            stderr_tail = proc.stderr.read() if proc.stderr else ""
            print(f"[spike] stderr tail:\n{stderr_tail[-3000:]}", flush=True)
            return 2
        print("[spike] sentinel present; tool is blocked. Capturing session id…", flush=True)
        # Give claude generous time to flush the transcript JSONL.
        # If a dangling tool_use isn't persisted even after a long wait,
        # that's the RFC's load-bearing assumption disproven.
        print("[spike] sleeping 10s to let claude flush transcript…", flush=True)
        time.sleep(10)

        # Give the early-session-id callback a chance.
        for _ in range(20):
            if "id" in early_session_id:
                break
            time.sleep(0.1)
        sid = early_session_id.get("id", "")
        if not sid:
            print("[spike] FAIL: never observed a session_id on the wire", flush=True)
            proc.kill()
            return 3

        # ----- Step 4: SIGKILL claude -----
        print(f"[spike] SIGKILL pid={proc.pid}", flush=True)
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=5)
        # Closing stdin/stdout to free fds.
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass

        # Locate the transcript.
        encoded = "-" + str(tmpdir).replace("/", "-")
        # `~/.claude/projects/<encoded>/<sid>.jsonl`
        transcripts = list(Path.home().glob(f".claude/projects/*{encoded[-30:]}/{sid}.jsonl"))
        if not transcripts:
            # Fallback: search by session id alone.
            transcripts = list(Path.home().glob(f".claude/projects/*/{sid}.jsonl"))
        if not transcripts:
            print(f"[spike] FAIL: couldn't find transcript for {sid}", flush=True)
            return 4
        transcript = transcripts[0]
        print(f"[spike] transcript: {transcript}", flush=True)

        pending = _find_pending_tool_use(transcript)
        if pending is None:
            print("[spike] FAIL: no pending tool_use in transcript — kill may have raced", flush=True)
            return 5
        sid_in_transcript, pending_tool_use_id = pending
        print(f"[spike] pending tool_use_id={pending_tool_use_id} in session={sid_in_transcript}", flush=True)

        # ----- Step 5-7: resume + inject tool_result -----
        # NO --mcp-config this time, so claude can't re-call the tool.
        resume_args = [
            "claude",
            "--resume", sid,
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--verbose",
            "--permission-mode", "bypassPermissions",
        ]
        print(f"[spike] $ {' '.join(resume_args)}", flush=True)
        rproc = subprocess.Popen(
            resume_args,
            cwd=str(tmpdir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=clean_env,
        )
        assert rproc.stdin is not None
        injection = {
            "type": "user",
            "session_id": "",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": pending_tool_use_id,
                        "content": "injected by spike-A",
                    }
                ],
            },
            "parent_tool_use_id": None,
        }
        print(f"[spike] injecting: {json.dumps(injection)[:300]}", flush=True)
        rproc.stdin.write(json.dumps(injection) + "\n")
        rproc.stdin.flush()
        rproc.stdin.close()

        # Drain output for up to 60s.
        out_lines = _read_stream_json_lines(rproc, max_lines=200, label="resume")
        try:
            rproc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            rproc.kill()
        stderr_tail = (rproc.stderr.read() if rproc.stderr else "")[-2000:]

        # ----- Decide pass/fail -----
        print("[spike] --- resumed claude stream-json output (first 30) ---", flush=True)
        for line in out_lines[:30]:
            print(f"[spike]   {json.dumps(line)[:300]}")
        print(f"[spike] --- stderr tail ---\n{stderr_tail}", flush=True)
        saw_result = any(m.get("type") == "result" for m in out_lines)
        saw_assistant_text = any(
            m.get("type") == "assistant"
            and any(b.get("type") == "text" for b in (m.get("message", {}).get("content", []) or []))
            for m in out_lines
        )
        verdict = "PASS" if (saw_result and saw_assistant_text) else "FAIL"
        print(f"[spike] VERDICT: {verdict} (saw_result={saw_result}, saw_assistant_text={saw_assistant_text})", flush=True)
        return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
