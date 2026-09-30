#!/usr/bin/env python3
"""Seed a dedicated sandbox app so the bench can run in `--mode live`.

Why this exists: `--mode dry-run` installs a MockSaasClient that answers every
call synthetically, so there is no login page and no Sign In button. Three
conversations in the corpus EDIT pre-existing objects, and in dry-run they are
structurally unwinnable — the agent looks, finds nothing, and correctly stops.
On 2026-09-03 `page-event-onclick` reasoned its way to "there is no `testapp`
app registered ... maybe this is a dry-run environment" and declined to patch a
button that does not exist. The oracle then failed it. That is not a quality
signal; it is the harness lying.

So live mode needs a real app with known contents. This writes exactly the
shapes the corpus asserts against, and rewrites them on every run so a bench is
reproducible rather than dependent on whatever the last run left behind.

  login   Grid > [TextBox email, TextBox password, Button "Sign In"]
  home    Grid > [Text heading, Button x3]        <- bulk-style-update needs
                                                     several Buttons to restyle

`ContactCFA` is deliberately NOT seeded: `end-to-end-new-page` creates it, and
its own setup_action deletes it first.

SAFETY: refuses to run against anything but a sandbox app code, and refuses a
non-local gateway outright. The bench deletes and rewrites pages; pointing it at
a real app would destroy work.

Usage:
    ./venv/bin/python scripts/seed_bench_sandbox.py            # seed
    ./venv/bin/python scripts/seed_bench_sandbox.py --verify   # report only
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

# The bench REWRITES these pages. Only ever point this at a throwaway app.
_ALLOWED_APP_CODES = {"benchsbx"}


def _req(gateway: str, method: str, path: str, token: str, app_code: str,
         client_code: str, body: dict | None = None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(gateway.rstrip("/") + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("appCode", app_code)
    req.add_header("clientCode", client_code)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw[:300]


def _grid(key, name, children, order=0, props=None):
    return {"key": key, "type": "Grid", "name": name, "displayOrder": order,
            "children": {c: True for c in children}, "properties": props or {},
            "styleProperties": {}}


def _leaf(key, ctype, name, order, props=None, styles=None):
    return {"key": key, "type": ctype, "name": name, "displayOrder": order,
            "properties": props or {}, "styleProperties": styles or {}}


def _page(name: str, app_code: str, client_code: str, children: list[dict], title: str) -> dict:
    """A page document in the shape `new_page_skeleton` produces."""
    root = _grid("root", "rootGrid", [c["key"] for c in children])
    definition = {"root": root}
    for c in children:
        definition[c["key"]] = c
    return {
        "name": name, "appCode": app_code, "clientCode": client_code,
        "rootComponent": "root", "componentDefinition": definition,
        "eventFunctions": {},
        "properties": {"title": {"name": {"value": title}}},
        "translations": {},
        "message": "Seeded by scripts/seed_bench_sandbox.py",
    }


def _pages(app_code: str, client_code: str) -> list[dict]:
    login = _page("login", app_code, client_code, [
        _leaf("email", "TextBox", "emailBox", 1, {"label": {"value": "Email"}}),
        _leaf("password", "TextBox", "passwordBox", 2, {"label": {"value": "Password"}}),
        # `page-event-onclick` addresses this button by its LABEL, so the label
        # text matters as much as the type.
        _leaf("signin", "Button", "signInButton", 3, {"label": {"value": "Sign In"}}),
    ], "Login")
    home = _page("home", app_code, client_code, [
        _leaf("heading", "Text", "heading", 1, {"text": {"value": "Home"}}),
        # Three of them: `bulk-style-update` says "change EVERY Button's
        # backgroundColor", which is only a meaningful assertion with several.
        _leaf("btnA", "Button", "primaryAction", 2, {"label": {"value": "Primary"}}),
        _leaf("btnB", "Button", "secondaryAction", 3, {"label": {"value": "Secondary"}}),
        _leaf("btnC", "Button", "tertiaryAction", 4, {"label": {"value": "Tertiary"}}),
    ], "Home")
    return [login, home]


def _existing(gateway, token, app_code, client_code) -> dict[str, str]:
    """{page name: id} already in the app."""
    status, body = _req(gateway, "GET",
                        f"/api/ui/pages?appCode={app_code}&size=200", token,
                        app_code, client_code)
    if status != 200 or not isinstance(body, dict):
        return {}
    return {
        p.get("name"): p.get("id")
        for p in body.get("content", []) if isinstance(p, dict) and p.get("name")
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gateway", default="http://localhost:8080")
    ap.add_argument("--token-file", default=str(_REPO / ".local-bench.jwt"))
    ap.add_argument("--app-code", default="benchsbx")
    ap.add_argument("--client-code", default="FIN")
    ap.add_argument("--verify", action="store_true", help="Report state, write nothing")
    args = ap.parse_args()

    if args.app_code not in _ALLOWED_APP_CODES:
        print(f"refusing: '{args.app_code}' is not a known sandbox app "
              f"({sorted(_ALLOWED_APP_CODES)}). The bench rewrites these pages.")
        return 2
    if "localhost" not in args.gateway and "127.0.0.1" not in args.gateway:
        print(f"refusing: {args.gateway} is not local. Seeding writes pages.")
        return 2

    token_path = Path(args.token_file)
    if not token_path.exists():
        print(f"no token at {token_path}. Mint one:\n"
              f"  curl -s -X POST {args.gateway}/api/security/authenticate \\\n"
              f"    -H 'Content-Type: application/json' -H 'appCode: appbuilder' \\\n"
              f"    -H 'clientCode: {args.client_code}' \\\n"
              f"    -d '{{\"userName\":\"<you>\",\"password\":\"<pw>\",\"rememberMe\":true}}'")
        return 2
    token = token_path.read_text().strip()

    have = _existing(args.gateway, token, args.app_code, args.client_code)
    if args.verify:
        print(f"app '{args.app_code}' pages: {sorted(have) or '(none)'}")
        return 0

    for page in _pages(args.app_code, args.client_code):
        name = page["name"]
        # Delete first so a re-seed is idempotent: the bench mutates these pages,
        # and a run that starts from the last run's leftovers is not a rerun.
        if name in have:
            _req(args.gateway, "DELETE", f"/api/ui/pages/{have[name]}", token,
                 args.app_code, args.client_code)
        status, body = _req(args.gateway, "POST", "/api/ui/pages", token,
                            args.app_code, args.client_code, page)
        ok = status < 400
        detail = "" if ok else f" -> {status} {str(body)[:140]}"
        n = len(page["componentDefinition"])
        print(f"  {'ok  ' if ok else 'FAIL'} {name:10s} {n} components{detail}")
        if not ok:
            return 1

    after = _existing(args.gateway, token, args.app_code, args.client_code)
    print(f"\napp '{args.app_code}' now has: {sorted(after)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
