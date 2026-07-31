#!/usr/bin/env python3
"""clone_harness.py — drive the appbuilder agent over its SSE chat endpoint.

One instruction per invocation. The agent runs fully autonomous (auto_confirm
pre-approves every mutating tool), so this harness just sends a message,
streams the SSE response, prints what the agent does, and surfaces any tool
failures so they can be fixed.

Auth: reads the bearer token from $MODLIX_TOKEN or scripts/.clone_token.
Session: the returned session_id is saved to scripts/.clone_session and reused
on the next call (use --new to start a fresh conversation).

Usage:
    python scripts/clone_harness.py "Clone https://iii.dev/ ..." --app iiiclone --new
    python scripts/clone_harness.py "Now build the footer too"      # resumes session
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib3
from pathlib import Path

import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HERE = Path(__file__).resolve().parent
TOKEN_FILE = HERE / ".clone_token"
SESSION_FILE = HERE / ".clone_session"
TRANSCRIPT_FILE = HERE / "clone_transcript.log"

# Hit the AI service directly: the nginx gateway in front of
# appbuilder.local.modlix.com closes long SSE connections (504) before a clone
# finishes. The service does its own JWT validation against the security
# service, so auth is identical either way. Override with --url if needed.
DEFAULT_URL = "http://localhost:5001/api/ai/appbuilder/chat"
CLIENT_CODE = "SYSTEM"
APP_CODE_HEADER = "appbuilder"  # which AI service, NOT the app being built


def _load_token() -> str:
    tok = os.environ.get("MODLIX_TOKEN", "").strip()
    if tok:
        return tok
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    sys.exit("No token: set $MODLIX_TOKEN or write scripts/.clone_token")


def _short(v, n=240) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + f" …(+{len(s) - n} chars)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("message", help="instruction for the agent")
    ap.add_argument("--app", default=None, help="target app_code to build into")
    ap.add_argument("--session", default=None, help="explicit session_id to resume")
    ap.add_argument("--new", action="store_true", help="ignore saved session, start fresh")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--read-timeout", type=float, default=240.0)
    args = ap.parse_args()

    token = _load_token()
    session_id = args.session
    if not session_id and not args.new and SESSION_FILE.exists():
        session_id = SESSION_FILE.read_text(encoding="utf-8").strip() or None

    headers = {
        "Authorization": f"Bearer {token}",
        "clientCode": CLIENT_CODE,
        "appCode": APP_CODE_HEADER,
        # The JWT is host-bound. When we bypass the gateway and hit :5001
        # directly, the gateway no longer injects these, so the service would
        # forward host=localhost:5001 to the security service and get a 401.
        # Set them to a host:port the token's `hostName`/`port` claims allow.
        "X-Forwarded-Host": "appbuilder.local.modlix.com",
        "X-Forwarded-Port": "443",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    body = {
        "message": args.message,
        "session_id": session_id,
        "app_code": args.app,
        "auto_confirm": True,
    }

    transcript = TRANSCRIPT_FILE.open("a", encoding="utf-8")
    transcript.write(f"\n\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} | session={session_id} | app={args.app} =====\n")
    transcript.write(f"USER: {args.message}\n")

    print(f"→ POST {args.url}  (session={session_id or 'NEW'}, app={args.app})")
    print(f"  {_short(args.message, 160)}\n")

    tools_ok = 0
    tools_failed: list[tuple[str, str]] = []
    text_buf: list[str] = []
    final_session = session_id
    t0 = time.time()

    try:
        resp = requests.post(
            args.url, headers=headers, json=body,
            stream=True, verify=False, timeout=(10, args.read_timeout),
        )
    except Exception as e:  # noqa: BLE001
        return _die(transcript, f"request failed: {type(e).__name__}: {e}")

    if resp.status_code != 200:
        snippet = ""
        try:
            snippet = resp.text[:600]
        except Exception:  # noqa: BLE001
            pass
        return _die(transcript, f"HTTP {resp.status_code}\n{snippet}")

    event = None
    for raw in resp.iter_lines(decode_unicode=True):
        if raw is None:
            continue
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        line = raw.strip()
        if not line:
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        try:
            data = json.loads(payload) if payload else {}
        except Exception:  # noqa: BLE001
            data = {"raw": payload}

        if event == "text":
            chunk = data.get("text", "")
            text_buf.append(chunk)
            sys.stdout.write(chunk)
            sys.stdout.flush()
        elif event == "thinking":
            pass  # reasoning stream — skip for brevity
        elif event == "tool_start":
            name = data.get("tool_name", "?")
            print(f"\n  🔧 {name}({_short(data.get('tool_input', {}), 200)})")
            transcript.write(f"TOOL_START {name} {_short(data.get('tool_input', {}), 500)}\n")
        elif event == "tool_result":
            name = data.get("tool_name", "?")
            ok = data.get("success", True)
            summary = data.get("summary", "")
            mark = "✅" if ok else "❌"
            print(f"  {mark} {name}: {_short(summary, 200)}")
            transcript.write(f"TOOL_RESULT {mark} {name} {_short(summary, 800)}\n")
            if ok:
                tools_ok += 1
            else:
                tools_failed.append((name, summary))
        elif event == "error":
            msg = data.get("message", payload)
            print(f"\n  ‼️  ERROR: {msg}")
            transcript.write(f"ERROR {msg}\n")
        elif event == "confirmation_request":
            # Should not happen with auto_confirm=True — flag if it does.
            print(f"\n  ⚠️  CONFIRMATION REQUESTED (auto_confirm not honored?): {_short(data, 200)}")
            transcript.write(f"CONFIRMATION_REQUEST {_short(data, 400)}\n")
        elif event == "done":
            final_session = data.get("session_id", final_session)
            usage = data.get("usage", {})
            print(f"\n\n── done ── session={final_session}")
            print(f"   usage: {_short(usage, 300)}")
            transcript.write(f"DONE session={final_session} usage={_short(usage, 400)}\n")
            break

    if final_session:
        SESSION_FILE.write_text(final_session, encoding="utf-8")

    elapsed = time.time() - t0
    print(f"\n── summary ── {elapsed:.0f}s | tools ok={tools_ok} failed={len(tools_failed)}")
    if tools_failed:
        print("   FAILED TOOLS:")
        for name, err in tools_failed:
            print(f"     ❌ {name}: {_short(err, 300)}")
    transcript.write(f"SUMMARY {elapsed:.0f}s ok={tools_ok} failed={len(tools_failed)}\n")
    transcript.close()
    return 2 if tools_failed else 0


def _die(transcript, msg: str) -> int:
    print(f"\n‼️  {msg}")
    try:
        transcript.write(f"FATAL {msg}\n")
        transcript.close()
    except Exception:  # noqa: BLE001
        pass
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
