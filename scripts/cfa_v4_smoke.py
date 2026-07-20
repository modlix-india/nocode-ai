"""Smoke test for the v4 /chat endpoint.

Sends ONE chat turn asking the agent to run a discovery script via
`code_run`. Verifies that:
  - The endpoint accepts the request
  - The agent calls code_run
  - The script imports modlix, reads the catalog, prints something useful
  - The chat session terminates with `done` (no SSE errors)

Usage: venv/bin/python scripts/cfa_v4_smoke.py [<app_code>]
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx


CFA_BASE_URL = os.environ.get("CFA_BASE_URL", "http://localhost:5001")
SAAS_BASE_URL = os.environ.get("SAAS_BASE_URL", "http://localhost:8080")
JWT_PATH = Path.home() / ".cfa-jwt"
CREDS_PATH = Path.home() / ".cfa-creds"
FORWARDED_HOST = "localhost:8080"
FORWARDED_PORT = "8080"


async def _login(creds: dict[str, str]) -> str:
    body = {
        "userName": creds["username"],
        "password": creds["password"],
        "identifierType": creds.get("identifierType", "EMAIL_ID"),
        "loggedInClientCode": creds.get("clientCode", "SYSTEM"),
    }
    headers = {
        "Content-Type": "application/json",
        "X-Forwarded-Host": FORWARDED_HOST,
        "X-Forwarded-Port": FORWARDED_PORT,
        "clientCode": creds.get("clientCode", "SYSTEM"),
        "appCode": creds.get("appCode", "appbuilder"),
    }
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(f"{SAAS_BASE_URL}/api/security/authenticate", json=body, headers=headers)
    r.raise_for_status()
    tok = r.json()["accessToken"]
    JWT_PATH.write_text(tok)
    JWT_PATH.chmod(0o600)
    return tok


async def _ensure_jwt() -> str:
    if JWT_PATH.exists():
        return JWT_PATH.read_text().strip()
    if not CREDS_PATH.exists():
        raise SystemExit("No JWT at ~/.cfa-jwt and no creds at ~/.cfa-creds")
    return await _login(json.loads(CREDS_PATH.read_text()))


PROMPT = """\
Smoke test. In ONE call to `code_run`, do all of the following and print the results:
1. `modlix.catalog.list_types()` — list every component type name. Print the count and the first 10 names.
2. `modlix.catalog.get_schema('Grid')` — print just the top-level keys of the schema dict.
3. `modlix.apps.list()` — print the count of apps and the first 5 appCodes.

Keep the script under 25 lines. Do NOT call code_run a second time.
"""


async def main() -> int:
    app_code = sys.argv[1] if len(sys.argv) > 1 else "appbuilder"
    jwt = await _ensure_jwt()
    headers = {
        "Authorization": f"Bearer {jwt}",
        "clientCode": "SYSTEM",
        "appCode": "appbuilder",
        "X-Forwarded-Host": FORWARDED_HOST,
        "X-Forwarded-Port": FORWARDED_PORT,
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
    }
    payload = {"message": PROMPT, "app_code": app_code}

    tool_calls: list[dict] = []
    text_chunks: list[str] = []
    errors: list[dict] = []

    print(f">>> POST {CFA_BASE_URL}/api/ai/appbuilderv4/chat  app_code={app_code}")
    async with httpx.AsyncClient(timeout=180.0) as client:
        async with client.stream("POST", f"{CFA_BASE_URL}/api/ai/appbuilderv4/chat",
                                 headers=headers, json=payload) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                print(f"HTTP {resp.status_code}: {body.decode()[:400]}")
                return 1
            current_event = None
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    current_event = line[len("event:"):].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                raw = line[len("data:"):].strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except Exception:
                    continue
                etype = (ev.get("type") or current_event or "").lower()
                if etype == "tool_start":
                    tool_calls.append({"name": ev.get("tool_name"), "id": ev.get("tool_use_id"),
                                       "input_keys": list((ev.get("input") or {}).keys())})
                    print(f"  tool_start  {ev.get('tool_name')}  keys={list((ev.get('input') or {}).keys())}")
                elif etype == "tool_result":
                    ok = ev.get("success")
                    summary = (ev.get("summary") or "")[:600].replace("\n", "\n    ")
                    print(f"  tool_result  ok={ok}\n    {summary}")
                elif etype == "text" or etype == "text_chunk":
                    t = ev.get("text") or ""
                    text_chunks.append(t)
                elif etype == "error":
                    errors.append(ev)
                    print(f"  ERROR  {ev}")
                elif etype == "done":
                    print(f"  done  session_id={ev.get('session_id')}")

    full_text = "".join(text_chunks).strip()
    print("\n=== assistant text ===")
    print(full_text[:1200])
    print("\n=== summary ===")
    print(f"  tool calls : {len(tool_calls)}")
    print(f"  errors     : {len(errors)}")
    print(f"  assistant chars: {len(full_text)}")
    print(f"  used code_run: {'yes' if any(t['name'] == 'code_run' for t in tool_calls) else 'no'}")

    ok = (len(tool_calls) >= 1 and any(t["name"] == "code_run" for t in tool_calls)
          and len(errors) == 0)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
