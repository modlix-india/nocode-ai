"""Scenario B — compose a hero section from CATALOG SCHEMAS ALONE.

No reference page. The agent must:
  1. Read `catalog.get_schema(...)` for the components it intends to use.
  2. Compose a page-definition dict from scratch.
  3. Write it via `pages.replace`.
  4. Verify the page contains all three pieces of copy.

Page target (v4hero in appbuilder):
  Root: a Grid with column layout
  Children:
    - Text  "Build apps with code"
    - Text  "Modlix v4 composes pages from Python"
    - Button "Get started"

Budget: 4 code_run calls. Success requires:
  - <=4 code_run calls
  - independent GET on v4hero contains ALL 3 strings
  - 0 SSE errors

Usage: venv/bin/python scripts/cfa_v4_hero.py
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

EXPECTED_STRINGS = [
    "Build apps with code",
    "Modlix v4 composes pages from Python",
    "Get started",
]


PROMPT = """\
Scenario B — compose a fresh page from the catalog WITHOUT cloning an
existing one. Budget: 4 code_run calls max.

Target: create a page named `v4hero` in the `appbuilder` app containing a
Grid root with three children, top to bottom:

  1. A Text with copy:  "Build apps with code"
  2. A Text with copy:  "Modlix v4 composes pages from Python"
  3. A Button with label: "Get started"

Use `modlix.catalog.get_schema('Grid')`, `get_schema('Text')`, and
`get_schema('Button')` to learn each component's properties.

Required steps (combine into ONE code_run when possible):

1. CLEANUP: list `appbuilder` pages and delete any prior `v4hero`
   (`modlix.delete('/api/ui/pages/' + p['id'])`). Ignore 404.

2. INSPECT three catalog schemas — print the `properties` keys for each so
   you know which property names map to "the visible text" on each
   component. (Hint from scenario A: Text uses `text`, with value
   wrapped as `{'value': '...'}`.)

3. CREATE the empty page: `modlix.post('/api/ui/pages', {appCode:
   'appbuilder', name: 'v4hero'})`. Don't add `permission` — that field
   is OPT-IN to restrict the page. Default (omitted) means public.

4. REPLACE with a full definition. Shape rules (from scenario A):
   - Each component is keyed by a UUID (use `modlix.uuid()`).
   - Each component has `key`, `name`, `type`, `properties`,
     `styleProperties`, and (for parents) `children: {childKey: True}`.
   - Property literal value: `{'value': '...'}`.
   - styleProperties keys are UUIDs; values are
     `{'resolutions': {'ALL': {'<cssProp>': {'value': '...'}}}}`.
   - The top-level page has `rootComponent: <uuid of root grid>` and
     `componentDefinition: {<key>: <component>, ...}`.

5. VERIFY: `modlix.pages.get('v4hero', app_code='appbuilder')`, print the
   componentDefinition (truncated 1500 chars), and confirm all three copy
   strings appear in the JSON.

If a step errors, print the traceback AND `e.response.text if e has one`
so we see what the platform rejected. Then make ONE corrective call.
"""


async def _login_fresh(creds: dict) -> str:
    body = {
        "userName": creds["username"],
        "password": creds["password"],
        "identifierType": creds.get("identifierType", "EMAIL_ID"),
        "loggedInClientCode": creds.get("clientCode", "SYSTEM"),
    }
    headers = {
        "Content-Type": "application/json",
        "X-Forwarded-Host": FORWARDED_HOST, "X-Forwarded-Port": FORWARDED_PORT,
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
        raise SystemExit("No JWT and no creds")
    return await _login_fresh(json.loads(CREDS_PATH.read_text()))


async def main() -> int:
    jwt = await _ensure_jwt()
    headers = {
        "Authorization": f"Bearer {jwt}",
        "clientCode": "SYSTEM",
        "appCode": "appbuilder",
        "X-Forwarded-Host": FORWARDED_HOST, "X-Forwarded-Port": FORWARDED_PORT,
        "Accept": "text/event-stream", "Content-Type": "application/json",
    }
    payload = {"message": PROMPT, "app_code": "appbuilder"}

    tool_calls: list[str] = []
    errors: list[dict] = []
    text_chunks: list[str] = []
    started = time.monotonic()

    print(f">>> POST {CFA_BASE_URL}/api/ai/appbuilderv4/chat  page=v4hero")
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
                    tool_calls.append(ev.get("tool_name", "?"))
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

    # Independent verification.
    print("\n=== independent verification ===")
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            list_resp = await c.get(f"{SAAS_BASE_URL}/api/ui/pages",
                                    params={"appCode": "appbuilder", "size": 200},
                                    headers={"Authorization": f"Bearer {jwt}",
                                             "clientCode": "SYSTEM", "appCode": "appbuilder",
                                             "X-Forwarded-Host": FORWARDED_HOST,
                                             "X-Forwarded-Port": FORWARDED_PORT})
            items = list_resp.json().get("content") or []
            target = next((p for p in items if p.get("name") == "v4hero"), None)
            if not target:
                print(f"  appbuilder has no 'v4hero' page (saw {len(items)} pages)")
                verified = False
            else:
                detail = await c.get(f"{SAAS_BASE_URL}/api/ui/pages/{target['id']}",
                                     headers={"Authorization": f"Bearer {jwt}",
                                              "clientCode": "SYSTEM", "appCode": "appbuilder",
                                              "X-Forwarded-Host": FORWARDED_HOST,
                                              "X-Forwarded-Port": FORWARDED_PORT})
                blob = json.dumps(detail.json())
                missing = [s for s in EXPECTED_STRINGS if s not in blob]
                print(f"  GET page detail → {detail.status_code}, size {len(blob):,} chars")
                for s in EXPECTED_STRINGS:
                    mark = "✓" if s in blob else "✗"
                    print(f"    {mark}  {s!r}")
                verified = not missing
    except Exception as e:  # noqa: BLE001
        print(f"  verification error: {type(e).__name__}: {e}")
        verified = False

    print("\n=== summary ===")
    print(f"  tool calls : {len(tool_calls)}  ({tool_calls})")
    print(f"  errors     : {len(errors)}")
    print(f"  assistant chars: {len(full_text)}")
    ok = (
        1 <= len(tool_calls) <= 4
        and all(t == "code_run" for t in tool_calls)
        and len(errors) == 0
        and verified
    )
    print(f"\n  scenario B: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
