"""Overnight exact-clone orchestrator.

Hits the v4 chat endpoint through the gateway, opens ONE long-running
session, and walks the agent through:
  1. Capture source screenshots + harvest assets.
  2. Build the clone region by region.
  3. Compare each region; iterate fixes until severity=high=0.
  4. Final full-page compare and any closing fixes.

Designed to run hands-off for hours. Logs everything to disk under
scripts/cfa_runs/v4_overnight/<ts>/ — transcript, snapshots, scorecards.

Pass criteria (logged but not enforced — this is a recording run, not a
gate): every compare_to_source returned `severity_counts['high'] == 0`
on its final invocation per region.

Usage:
  venv/bin/python scripts/cfa_v4_overnight.py [--target linear|stripe|vercel]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import os
import sys
import time
from pathlib import Path

import httpx


REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO_ROOT / "scripts" / "cfa_runs" / "v4_overnight"
CFA_BASE = os.environ.get("CFA_BASE_URL", "http://localhost:8080")  # gateway by default
SAAS_BASE = os.environ.get("SAAS_BASE_URL", "http://localhost:8080")
JWT_PATH = Path.home() / ".cfa-jwt"
CREDS_PATH = Path.home() / ".cfa-creds"
FORWARDED_HOST = "localhost:8080"
FORWARDED_PORT = "8080"

TARGETS: dict[str, dict[str, str]] = {
    "linear": {"url": "https://linear.app", "page_name": "home"},
    "stripe": {"url": "https://stripe.com", "page_name": "home"},
    "vercel": {"url": "https://vercel.com", "page_name": "home"},
}

# Letters-only appCode (platform requires it)
TARGET_APP = "vclone"


def _build_prompt(url: str, page_name: str) -> str:
    return f"""\
You're building an EXACT pixel-faithful clone of {url} as the page
`{page_name}` in the `{TARGET_APP}` Modlix app. This is one chunk of a
larger overnight job — there may already be partial progress from a
prior session.

KB FIRST (CRITICAL — don't reinvent platform knowledge):
- `platform_kb_search('clone external site', service='workflows')` and
  `platform_kb_get('workflows', '<slug>')` — there are 168 workflow
  recipes already curated. Search before composing call sequences.
- `platform_kb_search('<concept>', service='ui'|'security'|...)` — the
  gotchas + reference docs covering wrapShell, page permission, page
  id-vs-name lookups, Authorities valid values, etc.
- `kb_app_get('overview')` and `kb_app_get('current_focus')` and
  `kb_app_get('decisions_log')` for `{TARGET_APP}` — context from prior
  sessions on this exact app.
- When you discover something not in the KBs, `propose_kb_update` +
  `commit_kb_update` to the app's `decisions_log` so the next session
  doesn't pay the same cost.

Hard rules:
- The build target app is `{TARGET_APP}` (NOT appbuilder). All `code_run`
  calls authoring pages must pass `app_code='{TARGET_APP}'`.
- **Use REAL source imagery.** Call `extract_site_assets` and then, for
  EVERY Image component you author, pick the manifest entry by role +
  dimensions and bind its `modlix_url` as the `src.value`. NEVER invent
  URLs or use system-icon CDNs or `wikipedia.org` placeholders.
- **Use REAL source fonts.** Call `extract_site_fonts(url='{url}',
  app_code='{TARGET_APP}')` and PUT the returned `fontPacks_suggested`
  into `app.properties.fontPacks` BEFORE styling typography. Without
  this the browser falls back to system fonts and typography never
  matches. Workflow: `platform_kb_get('workflows', 'clone-with-real-assets-and-fonts')`.
- Pages in `{TARGET_APP}` are standalone — set `wrapShell: False`
  defensively. Do NOT set `permission` (omit = public; there's no
  generic public authority).

Step 0 — STATUS CHECK (do this first, always):
  ```python
  import modlix
  pages = modlix.pages.list(app_code='{TARGET_APP}')
  existing = [p for p in pages if p.get('name') == '{page_name}']
  if existing:
      cur = modlix.pages.get('{page_name}', app_code='{TARGET_APP}')
      print(f"page exists, version {{cur.get('version')}}, components={{len(cur.get('componentDefinition') or {{}})}}")
  else:
      print("page does not exist yet")
  ```
If the page already exists with >0 components, FOCUS on fixing remaining
diffs (skip extract_site_assets if the manifest is already in use, but
still do screenshot_external_url to get fresh handles for this session).
If the page doesn't exist, do a clean build.

Step 1 — capture: ONE call to:
  `screenshot_external_url(url='{url}',
   scroll_positions=[0.0, 0.33, 0.66, 1.0], viewport_width=1440)`
REMEMBER the four `source_handle` values; you need them per region.

Step 2 — harvest assets (skip if page already has Image bindings):
  `extract_site_assets(url='{url}', app_code='{TARGET_APP}', max_assets=80)`

Step 3 — for each region top-to-bottom (HERO first, then nav, mid-page,
FOOTER last):
  a. ONE code_run that creates/updates that region. Wrap rules:
       - property literals: `{{'value': '...'}}`
       - styleProperties keys are UUIDs (use `modlix.uuid()`)
       - children: `{{childKey: True}}` map
       - page-level `wrapShell: False`
     Use `modlix.pages.replace('{page_name}', <full def>,
     app_code='{TARGET_APP}')`.
  b. ONE `compare_to_source(page_name='{page_name}',
     source_handle=<handle covering this region>,
     app_code='{TARGET_APP}')`.
  c. Fix `severity=high` diffs in ONE more code_run, re-compare.
     Stop iterating this region after high=0 OR 5 compare rounds.

Step 4 — final full-page compare: ONE compare_to_source per scroll
position. List any remaining diffs. Declare done.

You may not finish in this session. Make as much measurable progress as
you can on the highest-priority remaining issues. The next session will
pick up where you stop.

Begin with Step 0.
"""


async def _login_fresh(creds: dict) -> str:
    body = {
        "userName": creds["username"], "password": creds["password"],
        "identifierType": creds.get("identifierType", "EMAIL_ID"),
        "loggedInClientCode": creds.get("clientCode", "SYSTEM"),
    }
    headers = {"Content-Type": "application/json",
               "X-Forwarded-Host": FORWARDED_HOST, "X-Forwarded-Port": FORWARDED_PORT,
               "clientCode": creds.get("clientCode", "SYSTEM"),
               "appCode": creds.get("appCode", "appbuilder")}
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(f"{SAAS_BASE}/api/security/authenticate", json=body, headers=headers)
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="linear", choices=sorted(TARGETS))
    args = ap.parse_args()
    target = TARGETS[args.target]
    url = target["url"]
    page_name = target["page_name"]

    ts = _dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    outdir = RUNS_DIR / args.target / ts
    outdir.mkdir(parents=True, exist_ok=True)
    transcript_path = outdir / "transcript.jsonl"
    summary_path = outdir / "summary.txt"

    print(f">>> v4 overnight clone")
    print(f"    target = {url}")
    print(f"    page   = {page_name} in app `{TARGET_APP}`")
    print(f"    out    = {outdir}")

    jwt = await _ensure_jwt()
    headers = {
        "Authorization": f"Bearer {jwt}",
        "clientCode": "SYSTEM", "appCode": "appbuilder",  # caller context — agent works against TARGET_APP via app_code parameter
        "X-Forwarded-Host": FORWARDED_HOST, "X-Forwarded-Port": FORWARDED_PORT,
        "Accept": "text/event-stream", "Content-Type": "application/json",
    }
    payload = {"message": _build_prompt(url, page_name), "app_code": TARGET_APP}

    tool_counts: dict[str, int] = {}
    errors: list[dict] = []
    text_chunks: list[str] = []
    severity_history: list[dict] = []  # one entry per compare_to_source call
    started = time.monotonic()

    with open(transcript_path, "w") as f:
        f.write(json.dumps({"event": "run_start", "url": url,
                            "page_name": page_name, "app_code": TARGET_APP,
                            "ts": ts}) + "\n")
        # Long timeout — overnight job.
        async with httpx.AsyncClient(timeout=None) as client:
            try:
                async with client.stream("POST",
                                         f"{CFA_BASE}/api/ai/appbuilderv4/chat",
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
                        ev["_etype"] = (ev.get("type") or current_event or "").lower()
                        f.write(json.dumps(ev, default=str) + "\n")
                        f.flush()
                        etype = ev["_etype"]
                        if etype == "tool_start":
                            name = ev.get("tool_name", "?")
                            tool_counts[name] = tool_counts.get(name, 0) + 1
                            print(f"  [{int(time.monotonic()-started):>6}s]  {name}")
                        elif etype == "tool_result":
                            name = ev.get("tool_name", "?")
                            summary = (ev.get("summary") or "")[:240].replace("\n", " ")
                            ok = ev.get("success")
                            print(f"           ↳ {name}  ok={ok}  {summary}")
                            if name == "compare_to_source" and ok:
                                # Extract severity counts from the textual summary
                                # produced by compare_to_source.
                                import re as _re
                                m = _re.search(r"high=(\d+)\s+medium=(\d+)\s+low=(\d+)",
                                               ev.get("summary") or "")
                                if m:
                                    sev = {"high": int(m.group(1)),
                                           "medium": int(m.group(2)),
                                           "low": int(m.group(3))}
                                    severity_history.append(sev)
                                    print(f"             severity {sev}")
                        elif etype in ("text", "text_chunk"):
                            text_chunks.append(ev.get("text") or "")
                        elif etype == "error":
                            errors.append(ev)
                            print(f"  ERROR  {ev}")
                        elif etype == "done":
                            print(f"  done  session={ev.get('session_id')}  "
                                  f"({(time.monotonic()-started)/60:.1f} min)")
            except Exception as e:  # noqa: BLE001
                print(f"\nStream error: {type(e).__name__}: {e}")
                errors.append({"error": str(e)})

        f.write(json.dumps({"event": "run_end",
                            "elapsed_seconds": time.monotonic() - started,
                            "tool_counts": tool_counts,
                            "severity_history": severity_history,
                            "errors": len(errors)}) + "\n")

    full_text = "".join(text_chunks).strip()
    summary_lines = [
        f"=== v4 overnight {args.target} ===",
        f"target  : {url}",
        f"page    : {page_name} in {TARGET_APP}",
        f"start   : {ts}",
        f"elapsed : {(time.monotonic()-started)/60:.1f} min",
        f"errors  : {len(errors)}",
        f"tool counts:",
    ]
    for name, n in sorted(tool_counts.items(), key=lambda kv: -kv[1]):
        summary_lines.append(f"  {n:>4}  {name}")
    summary_lines.append("")
    summary_lines.append("severity history (compare_to_source by order called):")
    for i, sev in enumerate(severity_history, 1):
        summary_lines.append(f"  #{i:>2}  high={sev['high']}  medium={sev['medium']}  low={sev['low']}")
    summary_lines.append("")
    summary_lines.append("=== final assistant text (first 4000 chars) ===")
    summary_lines.append(full_text[:4000])

    summary_path.write_text("\n".join(summary_lines))
    print()
    print("\n".join(summary_lines[:20]))
    print(f"\n  full summary → {summary_path}")
    print(f"  page preview → https://apps.local.modlix.com/{TARGET_APP}/SYSTEM/page/{page_name}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
