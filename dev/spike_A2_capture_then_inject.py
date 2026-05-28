"""Spike A2 — does the resumed claude re-emit the dangling tool_use
with a new id that we can inject a tool_result for?

Spike A established: after `--resume`, claude in stream-json mode
mints a new session_id and the original tool_use_id is not present in
the resumed session. But the conversation content IS preserved on disk.
Question: does claude re-emit the historical assistant turns (including
the dangling tool_use) into the resumed session's stream-json output
with NEW tool_use_ids?

If yes, the revive protocol becomes:
  1. --resume the parent
  2. Watch stream-json output
  3. When we see the dangling tool's name re-appear as a tool_use,
     capture its NEW tool_use_id
  4. Inject a tool_result for the new id
  5. Claude continues

If no (the resumed session is genuinely re-running the model and may
not make the same call), we fall back to MCP-side short-circuit.

Run:
  PYTHONPATH=plugins/callstack python dev/spike_A2_capture_then_inject.py
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


_DANGLING_TOOL_NAME = "mcp__blocker__block_then_ok"


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
    cfg.write_text(json.dumps({
        "mcpServers": {
            "blocker": {"command": sys.executable, "args": [str(server)]}
        }
    }))
    return cfg


def _clean_env() -> dict:
    return {
        k: v for k, v in os.environ.items()
        if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                     "CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH",
                     "CLAUDE_CODE_ENTRYPOINT")
    }


def _claude_args(*, mcp_cfg: Path | None = None, resume: str | None = None) -> list[str]:
    a = [
        "claude",
        "--output-format", "stream-json",
        "--input-format", "stream-json",
        "--verbose",
        "--permission-prompt-tool", "stdio",
        "--permission-mode", "bypassPermissions",
    ]
    if mcp_cfg is not None:
        a.extend(["--mcp-config", str(mcp_cfg)])
    if resume is not None:
        a.extend(["--resume", resume])
    return a


def _send(stdin, obj: dict) -> None:
    stdin.write(json.dumps(obj) + "\n")
    stdin.flush()


def _handshake(stdin) -> None:
    _send(stdin, {
        "type": "control_request",
        "request_id": f"req_init_{uuid.uuid4().hex[:8]}",
        "request": {"subtype": "initialize", "hooks": None},
    })


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="spike-A2-") as raw:
        tmp = Path(raw)
        sentinel = tmp / "sentinel"
        cfg = _mcp_cfg(tmp, _write_blocker(tmp))

        # ----- PHASE 1: get a real session with a dangling tool_use -----
        prompt = (
            f"Call the {_DANGLING_TOOL_NAME} tool with message='spike-A2'. "
            "Use it immediately. Don't say anything else."
        )
        env = _clean_env()
        proc = subprocess.Popen(
            _claude_args(mcp_cfg=cfg),
            cwd=str(tmp),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True, env=env,
        )
        assert proc.stdin and proc.stdout
        _handshake(proc.stdin)
        _send(proc.stdin, {
            "type": "user", "session_id": "",
            "message": {"role": "user", "content": prompt},
            "parent_tool_use_id": None,
        })
        captured_sid: dict = {}
        def drain1():
            assert proc.stdout
            for raw_line in proc.stdout:
                try:
                    msg = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                sid = msg.get("session_id")
                if sid and "id" not in captured_sid:
                    captured_sid["id"] = sid
                if msg.get("type") == "control_request":
                    req = msg.get("request", {})
                    if req.get("subtype") == "can_use_tool":
                        _send(proc.stdin, {
                            "type": "control_response",
                            "response": {
                                "subtype": "success",
                                "request_id": msg.get("request_id", ""),
                                "response": {"behavior": "allow", "updatedInput": req.get("input", {})},
                            },
                        })
        threading.Thread(target=drain1, daemon=True).start()

        # Wait for sentinel.
        t0 = time.monotonic()
        while not sentinel.exists() and time.monotonic() - t0 < 120:
            time.sleep(0.2)
        if not sentinel.exists():
            print("[spike] FAIL: sentinel never appeared")
            proc.kill()
            return 2
        print(f"[spike] sentinel hit, sleeping 10s for transcript flush", flush=True)
        time.sleep(10)
        sid = captured_sid.get("id", "")
        print(f"[spike] killing parent claude (sid={sid})", flush=True)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

        # ----- PHASE 2: --resume and WATCH for the dangling tool_use to re-emerge -----
        # NOTE: we keep --mcp-config here so the resumed claude is allowed
        # to use the tool again if it chooses. Our goal: capture the new
        # tool_use_id the moment it appears, intercept it BEFORE the MCP
        # tool actually executes (which would block forever again).
        rproc = subprocess.Popen(
            _claude_args(mcp_cfg=cfg, resume=sid),
            cwd=str(tmp),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env,
        )
        assert rproc.stdin and rproc.stdout

        # NOT sending a user message — just letting claude continue from
        # the transcript. Question: does claude continue without further
        # input? Or does it need a control_request init?
        # Try BOTH: send the init handshake, but no user message.
        _handshake(rproc.stdin)

        # Watch output: capture the first tool_use whose name matches the
        # dangling one. As soon as we see it, inject a tool_result via
        # control_response (deny + payload) — or via permission deny.
        # Actually: better to inject a fresh user tool_result message.
        captured: dict = {}
        observed_types: list[str] = []
        injected = threading.Event()

        def drain2():
            assert rproc.stdout
            for raw_line in rproc.stdout:
                try:
                    msg = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                mtype = msg.get("type", "?")
                observed_types.append(mtype)
                # Print compact.
                print(f"[spike resume] ← {mtype}: {json.dumps(msg)[:200]}", flush=True)
                # Watch for a can_use_tool control_request for the dangling tool.
                if mtype == "control_request":
                    req = msg.get("request", {})
                    if req.get("subtype") == "can_use_tool" and req.get("tool_name") == _DANGLING_TOOL_NAME:
                        # DENY the permission with a synthetic result. This
                        # is the "intercept at permission boundary" angle —
                        # we never let the tool actually run; we feed back
                        # an injected result via interrupted/deny.
                        print(f"[spike resume] intercepting can_use_tool for {_DANGLING_TOOL_NAME}", flush=True)
                        _send(rproc.stdin, {
                            "type": "control_response",
                            "response": {
                                "subtype": "success",
                                "request_id": msg.get("request_id", ""),
                                "response": {"behavior": "deny", "message": "INTERCEPTED: injected result = 'spike-A2 ok'"},
                            },
                        })
                        captured["intercepted"] = True
                    else:
                        # Other control_request — just succeed.
                        _send(rproc.stdin, {
                            "type": "control_response",
                            "response": {
                                "subtype": "success",
                                "request_id": msg.get("request_id", ""),
                                "response": {},
                            },
                        })
                if mtype == "assistant":
                    for b in msg.get("message", {}).get("content", []) or []:
                        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == _DANGLING_TOOL_NAME:
                            captured.setdefault("dangling_id", b["id"])
                            print(f"[spike resume] dangling tool_use re-emerged with id={b['id']}", flush=True)
                if mtype == "user":
                    # Look for the tool_result that resolves the dangling tool_use.
                    for b in msg.get("message", {}).get("content", []) or []:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            print(f"[spike resume]   tool_result for {b.get('tool_use_id')}: {str(b.get('content'))[:120]}", flush=True)
                if mtype == "result":
                    captured["result_seen"] = True

        t = threading.Thread(target=drain2, daemon=True)
        t.start()

        # Wait up to 90s for a result or for the dangling tool to re-emerge.
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if captured.get("result_seen"):
                break
            if rproc.poll() is not None:
                break
            time.sleep(0.5)

        try:
            rproc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            rproc.kill()

        print(f"[spike] captured: {captured}", flush=True)
        print(f"[spike] observed types: {observed_types[:60]}", flush=True)
        if captured.get("intercepted") and captured.get("result_seen"):
            print("[spike] VERDICT: PASS — can_use_tool deny works as a synthetic-result injection point", flush=True)
            return 0
        elif "dangling_id" in captured:
            print("[spike] VERDICT: PARTIAL — dangling tool re-emerged but interception incomplete", flush=True)
            return 1
        else:
            print("[spike] VERDICT: FAIL — dangling tool did not re-emerge in resumed session", flush=True)
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
