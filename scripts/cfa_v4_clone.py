"""Scenario C — clone linear.app's hero region into a Modlix page using
the vision compare loop.

The agent should:
  1. Screenshot linear.app (top-of-page only is enough for the hero).
     Remember the returned source_handle.
  2. Create a fresh page `v4linearhero` in `appbuilder`.
  3. Compose a hero region via code_run: gradient background, the linear
     headline, sub-copy, primary CTA.
  4. Call compare_to_source(page=v4linearhero, source_handle=top-shot).
  5. Apply fixes for any severity=high diffs in ONE follow-up code_run.
  6. Re-compare. Stop when severity=high == 0 OR after 3 compare rounds.

Success criteria:
  - The page exists in appbuilder.
  - At least one compare_to_source call ran.
  - Final compare's severity_counts['high'] == 0.
  - Total turns <= 12.

Usage: venv/bin/python scripts/cfa_v4_clone.py
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
Scenario C — clone the hero region of https://linear.app into a Modlix
page. Budget: 10 tool calls maximum (mix of screenshot_external_url,
compare_to_source, code_run).

Plan (follow this order):

1. `screenshot_external_url(url='https://linear.app', scroll_positions=[0.0])`
   to capture the top-of-page. ONE shot is enough for the hero. Note the
   `source_handle` it returns — you'll pass it to compare_to_source later.
   Look at the attached PNG — that is your visual spec.

2. In ONE `code_run`: clean up any prior `v4linearhero` page in `appbuilder`,
   create a fresh `v4linearhero` page (`modlix.post('/api/ui/pages', {...})`),
   then compose a hero definition. The hero should have:
   - Dark background matching the source (Linear uses a near-black
     `#0a0a0a` style).
   - Headline copy verbatim from what you see (Linear typically reads
     "The product development system" or similar — use what's visible in
     the screenshot).
   - Sub-copy below the headline.
   - One or two CTA Buttons (`Sign up` / `Log in` / `Contact sales`).
   - Use `modlix.pages.replace('v4linearhero', new_def, app_code='appbuilder')`.

3. `compare_to_source(page_name='v4linearhero', source_handle=<handle>)`
   to diff your build vs the source. Look at the build PNG attached to
   the result.

4. For every `severity=high` entry, apply the smallest possible fix in
   ONE `code_run` (use patches against the page definition you already
   have in mind). Then re-call compare_to_source.

5. Stop iterating when `severity_counts['high'] == 0` OR after 3 compare
   rounds — whichever comes first.

Print clear progress markers between steps. When you stop, summarize the
final state and the remaining medium/low diffs.
"""


async def _login_fresh(creds: dict) -> str:
    body = {
        "userName": creds["username"], "password": creds["password"],
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
    JWT_PATH.write_text(tok); JWT_PATH.chmod(0o600)
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
        "clientCode": "SYSTEM", "appCode": "appbuilder",
        "X-Forwarded-Host": FORWARDED_HOST, "X-Forwarded-Port": FORWARDED_PORT,
        "Accept": "text/event-stream", "Content-Type": "application/json",
    }
    payload = {"message": PROMPT, "app_code": "appbuilder"}

    tool_calls: list[str] = []
    errors: list[dict] = []
    text_chunks: list[str] = []
    final_severity: dict = {}
    started = time.monotonic()

    print(f">>> POST {CFA_BASE_URL}/api/ai/appbuilderv4/chat  page=v4linearhero")
    async with httpx.AsyncClient(timeout=600.0) as client:
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
                    summary = (ev.get("summary") or "")[:700].replace("\n", "\n    ")
                    if ev.get("tool_name") == "compare_to_source":
                        # Extract severity counts from the summary line.
                        # The tool puts "Severity: high=X medium=Y low=Z" early.
                        import re as _re
                        m = _re.search(r"high=(\d+)\s+medium=(\d+)\s+low=(\d+)", summary)
                        if m:
                            final_severity = {"high": int(m.group(1)),
                                              "medium": int(m.group(2)),
                                              "low": int(m.group(3))}
                    print(f"  tool_result  ok={ok}  {ev.get('tool_name')}\n    {summary}")
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

    # Independent verification: page must exist with non-trivial definition.
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
            target = next((p for p in items if p.get("name") == "v4linearhero"), None)
            if not target:
                print(f"  appbuilder has no 'v4linearhero' page")
                verified = False
            else:
                detail = await c.get(f"{SAAS_BASE_URL}/api/ui/pages/{target['id']}",
                                     headers={"Authorization": f"Bearer {jwt}",
                                              "clientCode": "SYSTEM", "appCode": "appbuilder",
                                              "X-Forwarded-Host": FORWARDED_HOST,
                                              "X-Forwarded-Port": FORWARDED_PORT})
                doc = detail.json()
                comp_def = doc.get("componentDefinition") or {}
                num_comp = len(comp_def)
                blob = json.dumps(doc)
                print(f"  GET v4linearhero → {detail.status_code}, "
                      f"{len(blob):,} chars, {num_comp} components")
                verified = num_comp >= 3
    except Exception as e:  # noqa: BLE001
        print(f"  verification error: {type(e).__name__}: {e}")
        verified = False

    used_compare = "compare_to_source" in tool_calls
    used_screenshot = "screenshot_external_url" in tool_calls
    print("\n=== summary ===")
    print(f"  tool calls : {len(tool_calls)}  ({tool_calls})")
    print(f"  errors     : {len(errors)}")
    print(f"  used screenshot_external_url: {used_screenshot}")
    print(f"  used compare_to_source     : {used_compare}")
    print(f"  final severity counts      : {final_severity}")

    ok = (
        len(tool_calls) <= 12
        and used_screenshot
        and used_compare
        and len(errors) == 0
        and verified
        and final_severity.get("high", 99) == 0
    )
    print(f"\n  scenario C: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
