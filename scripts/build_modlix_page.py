#!/usr/bin/env python3
"""Build a real Modlix page from a page-analyzer analysis.json.

Maps the captured full DOM tree -> a Modlix componentDefinition and upserts it
to /api/ui/pages (POST to create, PUT to replace by name). Posts the definition
directly because a full-page component map is too large to inline as an MCP
tool argument.

Usage:
    ./venv/bin/python scripts/build_modlix_page.py --in runs/page_analyzer/iii_final/analysis.json \
        --app appbuilder --name iiiclone
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import urllib3

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests  # noqa: E402

from app.services.page_analyzer.models import PageAnalysis  # noqa: E402
from app.services.page_analyzer.to_page_definition import build_page_definition  # noqa: E402

urllib3.disable_warnings()

GATEWAY = os.environ.get("MODLIX_GATEWAY_URL", "https://apps.local.modlix.com")
CLIENT = os.environ.get("MODLIX_DEFAULT_CLIENT_CODE", "SYSTEM")
HOST_PATTERN = os.environ.get("MODLIX_HOST_PATTERN", "{app}.local.modlix.com")
TOKEN = ""  # filled by login()


def load_env_file(path: Path) -> None:
    """Read KEY=VALUE lines into os.environ without overwriting what is set.

    The credentials used to belong to this file as literals, which meant the
    script only ever worked for one account on one machine: when that account
    went INACTIVE the script died at login with a 403 and no way to point it
    somewhere else. modlix-mcp keeps a working identity in its own `.env`, so
    read that rather than carrying a second copy.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def login(user: str, password: str) -> str:
    """Authenticate as a real user (full authorities) — the authoring JWT is
    app-scoped and 403s on app access. Mirrors the CFA's _login_one_shot.

    Credentials WIN over a ready-made MODLIX_TOKEN, which is the opposite of
    what most tools here do, for one reason: the stored authoring token is
    app-scoped. modlix-mcp's is pinned to `appbuilder`, so writing a page into
    any other app answers `403 Cannot create Page for the selected client` even
    though the identity behind it is a SYSTEM user who plainly may. A fresh
    credential login is not pinned. MODLIX_TOKEN stays as the fallback for when
    there are no credentials to hand.
    """
    if not user or not password:
        ready = os.environ.get("MODLIX_TOKEN", "").strip()
        if ready:
            print("login: no credentials, falling back to MODLIX_TOKEN "
                  "(app-scoped: writes outside its own app will 403)")
            return ready
        sys.exit("login: no credentials and no MODLIX_TOKEN. Pass --user/--password "
                 "or point --env-file at an .env carrying MODLIX_USERNAME/PASSWORD.")
    r = requests.post(
        f"{GATEWAY}/api/security/authenticate",
        json={"userName": user, "password": password, "rememberMe": False},
        verify=False, timeout=20,
    )
    if r.status_code >= 400:
        sys.exit(f"login: HTTP {r.status_code} for {user}: {r.text[:200]}")
    body = r.json()
    tok = body.get("accessToken") or body.get("AuthToken") or body.get("token")
    if not tok:
        sys.exit(f"login: no token in response keys={list(body.keys())}")
    return tok


def _headers(app: str) -> dict:
    """The app context is the HOST, not the appCode field.

    This used to send `X-Forwarded-Host: appbuilder.local.modlix.com` for every
    app, so a body saying `appCode: iiidev` was written against appbuilder's
    context and answered `403 Cannot create Page for the selected client`. The
    header has to name the app being written to.
    """
    return {
        "Authorization": f"Bearer {TOKEN}",
        "clientCode": CLIENT,
        "appCode": app,
        "X-Forwarded-Host": HOST_PATTERN.format(app=app),
        "X-Forwarded-Port": "443",
        "Content-Type": "application/json",
    }


def _rows(data):
    if isinstance(data, list):
        return data
    return data.get("content") or data.get("data") or []


def upsert_global_style(app: str, name: str, analysis) -> None:
    """Create/replace an app-level CSS doc with :root vars + @font-face +
    @keyframes so var() colors, the web font, and animations resolve. These are
    safe globals (no universal selectors) so they don't restyle the builder."""
    root_vars = analysis.root_custom_properties or {}
    parts = []
    if root_vars:
        parts.append(":root {\n" + "\n".join(f"  {k}: {v};" for k, v in root_vars.items()) + "\n}")
    parts.extend(analysis.font_faces or [])
    parts.extend(analysis.keyframes or [])
    css = "\n".join(parts)
    if not css.strip():
        return
    base = f"{GATEWAY}/api/ui/styles"
    h = _headers(app)
    existing = None
    r = requests.get(base, headers=h, params={"appCode": app, "name": name, "size": 50}, verify=False, timeout=30)
    if r.status_code < 400:
        for s in _rows(r.json()):
            if isinstance(s, dict) and s.get("name") == name:
                existing = s
                break
    if existing:
        existing["styleString"] = css
        existing["message"] = "globals from analysis.json"
        resp = requests.put(f"{base}/{existing.get('id')}", headers=h, json=existing, verify=False, timeout=60)
    else:
        body = {"name": name, "appCode": app, "clientCode": CLIENT, "styleString": css, "message": "globals from analysis.json"}
        resp = requests.post(base, headers=h, json=body, verify=False, timeout=60)
    print(f"global style '{name}' -> HTTP {resp.status_code} ({len(css)} bytes CSS)")


def find_page_id(app: str, name: str):
    r = requests.get(
        f"{GATEWAY}/api/ui/pages", headers=_headers(app),
        params={"name": name, "size": 20, "appCode": app}, verify=False, timeout=30,
    )
    if r.status_code != 200:
        return None
    for row in _rows(r.json()):
        if isinstance(row, dict) and row.get("name") == name:
            return row.get("id")
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--app", default="appbuilder")
    ap.add_argument("--name", default="iiiclone")
    ap.add_argument("--cap", type=int, default=6000)
    ap.add_argument("--env-file", default=str(Path.home() / "kiran/fincity/modlix-mcp/.env"),
                    help="KEY=VALUE file supplying MODLIX_TOKEN or MODLIX_USERNAME/PASSWORD")
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    args = ap.parse_args()

    load_env_file(Path(args.env_file))
    user = args.user or os.environ.get("MODLIX_USERNAME", "")
    password = args.password or os.environ.get("MODLIX_PASSWORD", "")

    global GATEWAY, CLIENT, HOST_PATTERN, TOKEN
    GATEWAY = os.environ.get("MODLIX_GATEWAY_URL", GATEWAY)
    CLIENT = os.environ.get("MODLIX_DEFAULT_CLIENT_CODE", CLIENT)
    HOST_PATTERN = os.environ.get("MODLIX_HOST_PATTERN", HOST_PATTERN)
    TOKEN = login(user, password)
    print(f"gateway {GATEWAY}, identity {'MODLIX_TOKEN' if not user else user}")

    with open(args.inp, encoding="utf-8") as fh:
        analysis = PageAnalysis(**json.load(fh))
    if analysis.full_tree is None:
        sys.exit("analysis.json has no full_tree (run --stage full)")

    comps, root = build_page_definition(
        analysis.full_tree, cap=args.cap, css_vars=analysis.root_custom_properties,
    )
    print(f"built {len(comps)} components, root={root}")

    pid = find_page_id(args.app, args.name)
    if pid:
        r = requests.get(f"{GATEWAY}/api/ui/pages/{pid}", headers=_headers(args.app), verify=False, timeout=30)
        r.raise_for_status()
        page = r.json()
        page["rootComponent"] = root
        page["componentDefinition"] = comps
        page.setdefault("properties", {})["wrapShell"] = False  # render bare, no IDE shell
        page["message"] = "from analysis.json"
        resp = requests.put(
            f"{GATEWAY}/api/ui/pages/{pid}", headers=_headers(args.app), json=page, verify=False, timeout=180,
        )
        action = f"PUT (id={pid})"
    else:
        page = {
            "name": args.name, "appCode": args.app, "clientCode": CLIENT,
            "rootComponent": root, "componentDefinition": comps, "eventFunctions": {},
            "properties": {"title": {"name": {"value": args.name}}, "wrapShell": False},
            "translations": {},
            "message": "from analysis.json",
        }
        resp = requests.post(
            f"{GATEWAY}/api/ui/pages", headers=_headers(args.app), json=page, verify=False, timeout=180,
        )
        action = "POST (create)"

    print(f"{action} -> HTTP {resp.status_code}")
    if resp.status_code >= 300:
        print(resp.text[:1000])
        sys.exit(1)
    body = resp.json()
    print("page id:", body.get("id"))
    upsert_global_style(args.app, args.name + "Globals", analysis)
    print(f"PREVIEW: {GATEWAY}/{args.app}/SYSTEM/page/{args.name}")


if __name__ == "__main__":
    main()
