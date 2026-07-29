"""Scenario A — hello-world end-to-end loop for v4.

Asks the agent to do an ENTIRE read-mutate-write cycle in code_run:
  1. Find a reference page (any existing page) to learn the canonical shape.
  2. Create a fresh sandbox app `v4hello` (deleted first if it exists).
  3. Create a `home` page in the new app.
  4. Compose a page definition containing one TextLabel with text
     "Modlix v4 hello".
  5. Replace the page's definition (atomic PUT).
  6. Re-fetch and print proof that the text landed.

Success signals:
  - The agent uses code_run (1-3 calls).
  - The final assistant text contains "Modlix v4 hello".
  - We can independently verify via the gateway that
    `https://apps.local.modlix.com/v4hello/SYSTEM/page/home` renders the
    label.

Usage:
    venv/bin/python scripts/cfa_v4_hello.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx


CFA_BASE_URL = os.environ.get("CFA_BASE_URL", "http://localhost:5001")
SAAS_BASE_URL = os.environ.get("SAAS_BASE_URL", "http://localhost:8080")
JWT_PATH = Path.home() / ".cfa-jwt"
CREDS_PATH = Path.home() / ".cfa-creds"
FORWARDED_HOST = "localhost:8080"
FORWARDED_PORT = "8080"


PROMPT = """\
Scenario A — page read-mutate-write loop in the EXISTING `appbuilder` app.
This scenario does NOT touch app creation; it proves the page mutation path
end to end.

Budget: 5 code_run calls maximum.

Steps (combine into ONE code_run when possible):

1. CLEANUP — ensure no stale `v4hello` page exists in `appbuilder`:
   List existing pages: `modlix.pages.list(app_code='appbuilder')`.
   For any page whose `name == 'v4hello'`, delete it via
   `modlix.delete('/api/ui/pages/' + p['id'])`. Ignore 404.

2. DISCOVER the canonical page shape:
   Pick a real page from the list (e.g. `home`). Fetch it:
   `modlix.pages.get('home', app_code='appbuilder')`.
   Print the top-level keys and the first 1200 chars of `componentDefinition`
   so you can see the wrap conventions.

3. CREATE a fresh page `v4hello` in `appbuilder`:
   ```python
   create_resp = modlix.post('/api/ui/pages', {
       'appCode': 'appbuilder',
       'name': 'v4hello',
       # Omit `permission` for public pages — only set it to gate access.
   })
   ```
   The response includes the new page's Mongo `id`. Save it.

4. REPLACE the page definition with a single root component that has ONE
   child of type `Text` (NOT 'TextLabel' — confirm via the reference page
   you fetched above) whose text/label property value is exactly
   `"Modlix v4 hello"`. Follow the wrap rules you saw in the reference:
     - property literals wrap as `{'value': '...'}`
     - styleProperties keys are UUIDs via `modlix.uuid()`
     - the page-level `rootComponent` must point at the root component key
   Call `modlix.pages.replace('v4hello', new_definition, app_code='appbuilder')`.

5. VERIFY:
   `modlix.pages.get('v4hello', app_code='appbuilder')` — print the
   `componentDefinition` (truncated to 1500 chars). Confirm `"Modlix v4 hello"`
   appears somewhere in the JSON.

If anything fails, print the full traceback AND the response body from the
last HTTP call. Then make ONE corrective code_run.
"""


async def _login_fresh(creds: dict[str, str]) -> str:
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
    return await _login_fresh(json.loads(CREDS_PATH.read_text()))


async def main() -> int:
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
    payload = {"message": PROMPT, "app_code": "appbuilder"}

    tool_calls: list[dict] = []
    text_chunks: list[str] = []
    errors: list[dict] = []
    started = time.monotonic()

    print(f">>> POST {CFA_BASE_URL}/api/ai/appbuilderv4/chat  app_code=appbuilder  (target page=v4hello)")
    async with httpx.AsyncClient(timeout=300.0) as client:
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
                    tool_calls.append({"name": ev.get("tool_name"), "id": ev.get("tool_use_id")})
                    print(f"  tool_start  {ev.get('tool_name')}")
                elif etype == "tool_result":
                    ok = ev.get("success")
                    summary = (ev.get("summary") or "")[:600].replace("\n", "\n    ")
                    print(f"  tool_result  ok={ok}\n    {summary}")
                elif etype in ("text", "text_chunk"):
                    text_chunks.append(ev.get("text") or "")
                elif etype == "error":
                    errors.append(ev)
                    print(f"  ERROR  {ev}")
                elif etype == "done":
                    print(f"  done  session_id={ev.get('session_id')}  ({time.monotonic()-started:.1f}s)")

    full_text = "".join(text_chunks).strip()
    print("\n=== assistant text ===")
    print(full_text[:1500])
    print("\n=== summary ===")
    print(f"  tool calls : {len(tool_calls)}  ({[t['name'] for t in tool_calls]})")
    print(f"  errors     : {len(errors)}")
    print(f"  assistant chars: {len(full_text)}")
    print(f"  contains 'Modlix v4 hello' in agent text: {'Modlix v4 hello' in full_text}")

    # Independent verification: hit the gateway directly. Platform's page
    # detail endpoint is by Mongo id, not by name — so we list first, find
    # the entry whose name=='home', then fetch the detail.
    print("\n=== independent verification ===")
    try:
        verify_headers = {
            "Authorization": f"Bearer {jwt}",
            "clientCode": "SYSTEM",
            "appCode": "appbuilder",
            "X-Forwarded-Host": FORWARDED_HOST,
            "X-Forwarded-Port": FORWARDED_PORT,
        }
        async with httpx.AsyncClient(timeout=15.0) as c:
            list_resp = await c.get(f"{SAAS_BASE_URL}/api/ui/pages",
                                    params={"appCode": "appbuilder", "size": 200},
                                    headers=verify_headers)
            if list_resp.status_code != 200:
                print(f"  LIST pages → {list_resp.status_code}: {list_resp.text[:200]}")
                verified = False
            else:
                items = list_resp.json().get("content") or []
                target = next((p for p in items if p.get("name") == "v4hello"), None)
                if not target:
                    print(f"  appbuilder has no 'v4hello' page (saw {len(items)} pages)")
                    verified = False
                else:
                    detail_resp = await c.get(f"{SAAS_BASE_URL}/api/ui/pages/{target['id']}",
                                              headers=verify_headers)
                    if detail_resp.status_code != 200:
                        print(f"  GET page detail → {detail_resp.status_code}: {detail_resp.text[:200]}")
                        verified = False
                    else:
                        page_json_str = json.dumps(detail_resp.json())
                        has_text = "Modlix v4 hello" in page_json_str
                        print(f"  GET page detail → 200, size {len(page_json_str):,} chars")
                        print(f"  'Modlix v4 hello' found in page JSON: {has_text}")
                        verified = has_text
    except Exception as e:  # noqa: BLE001
        print(f"  verification error: {type(e).__name__}: {e}")
        verified = False

    ok = (
        len(tool_calls) >= 1
        and any(t["name"] == "code_run" for t in tool_calls)
        and len(tool_calls) <= 5
        and len(errors) == 0
        and verified
    )
    print(f"\n  scenario A: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
