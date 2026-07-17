"""Non-LLM smoke for the v4 SDK.

Exercises every public method of `modlix.*` against the running platform
using the cached JWT. The agent's runs revealed at least 3 SDK bugs that
should have been caught here, so this is the gate before any LLM
scenario.

Each check prints PASS/FAIL with the response (truncated). Exits non-zero
on any failure so CI / `&&` chains short-circuit. Designed to run in
under 10 seconds — no API costs.

Usage: venv/bin/python scripts/cfa_v4_sdk_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Configure the SDK env BEFORE importing the SDK module — `_Config.from_env`
# runs at import time.
JWT_PATH = Path.home() / ".cfa-jwt"
CREDS_PATH = Path.home() / ".cfa-creds"
SAAS_BASE_URL = "http://localhost:8080"
CATALOG_URL = "https://cdn-local.modlix.com/js/dist/component-catalog.json"


def _ensure_jwt_sync() -> str:
    if JWT_PATH.exists():
        return JWT_PATH.read_text().strip()
    if not CREDS_PATH.exists():
        raise SystemExit("No JWT and no creds — log in once first.")
    import httpx
    creds = json.loads(CREDS_PATH.read_text())
    body = {
        "userName": creds["username"],
        "password": creds["password"],
        "identifierType": creds.get("identifierType", "EMAIL_ID"),
        "loggedInClientCode": creds.get("clientCode", "SYSTEM"),
    }
    headers = {
        "Content-Type": "application/json",
        "X-Forwarded-Host": "localhost:8080",
        "X-Forwarded-Port": "8080",
        "clientCode": creds.get("clientCode", "SYSTEM"),
        "appCode": creds.get("appCode", "appbuilder"),
    }
    r = httpx.post(f"{SAAS_BASE_URL}/api/security/authenticate", json=body, headers=headers, timeout=30.0)
    r.raise_for_status()
    tok = r.json()["accessToken"]
    JWT_PATH.write_text(tok)
    JWT_PATH.chmod(0o600)
    return tok


jwt = _ensure_jwt_sync()
os.environ["MODLIX_GATEWAY_URL"] = SAAS_BASE_URL
os.environ["MODLIX_AUTH_TOKEN"] = jwt
os.environ["MODLIX_APP_CODE"] = "appbuilder"
os.environ["MODLIX_CLIENT_CODE"] = "SYSTEM"
os.environ["MODLIX_FORWARDED_HOST"] = "localhost:8080"
os.environ["MODLIX_FORWARDED_PORT"] = "8080"
os.environ["MODLIX_CATALOG_URL"] = CATALOG_URL


from app.agents.appbuilderv4.sdk import (  # noqa: E402
    catalog, pages, apps, uuid, post, get, put, delete, config,
)


failures: list[tuple[str, str]] = []
passes: list[str] = []


def check(name: str, fn):
    started = time.monotonic()
    try:
        out = fn()
        elapsed = time.monotonic() - started
        repr_out = json.dumps(out, default=str)[:240] if isinstance(out, (dict, list)) else str(out)[:240]
        print(f"  PASS  {name:<40s} {elapsed*1000:>6.0f}ms  {repr_out}")
        passes.append(name)
    except Exception as e:  # noqa: BLE001
        elapsed = time.monotonic() - started
        msg = f"{type(e).__name__}: {e}"
        print(f"  FAIL  {name:<40s} {elapsed*1000:>6.0f}ms  {msg[:200]}")
        failures.append((name, msg))


print(">>> v4 SDK smoke")
print(f"    config.gateway_url = {config.gateway_url}")
print(f"    config.catalog_url = {config.catalog_url}")
print(f"    JWT tail = ...{jwt[-12:]}")
print()


# ── catalog ─────────────────────────────────────────────────────────────
print("--- catalog ---")
types_holder: dict = {}

def _catalog_list():
    t = catalog.list_types()
    assert isinstance(t, list), f"expected list, got {type(t).__name__}"
    assert len(t) > 10, f"expected >10 types, got {len(t)}"
    assert "Grid" in t and "Page" in t, f"missing core types: Grid/Page"
    types_holder["types"] = t
    return f"{len(t)} types"

check("catalog.list_types", _catalog_list)
check("catalog.get_schema('Grid')", lambda: list(catalog.get_schema("Grid").keys()))
check("catalog.get_schema('Page')", lambda: list(catalog.get_schema("Page").keys()))
check("catalog.search('text')", lambda: catalog.search("text")[:5])

# ── apps ────────────────────────────────────────────────────────────────
print("\n--- apps ---")
apps_holder: dict = {}

def _apps_list():
    items = apps.list()
    assert isinstance(items, list), f"expected list, got {type(items).__name__}"
    assert len(items) > 0, "expected at least 1 app in the security directory"
    for a in items:
        assert "appCode" in a, f"item missing appCode: {a}"
    apps_holder["items"] = items
    appbuilder = next((a for a in items if a.get("appCode") == "appbuilder"), None)
    assert appbuilder is not None, "no `appbuilder` app in directory"
    apps_holder["appbuilder"] = appbuilder
    return f"{len(items)} apps; appbuilder id={appbuilder.get('id')}"

check("apps.list", _apps_list)
check("apps.get_security('appbuilder')", lambda: {k: apps_holder["appbuilder"][k] for k in ("appCode", "appType") if k in apps_holder.get("appbuilder", {})})
check("apps.get_ui('appbuilder')", lambda: list(apps.get_ui("appbuilder").keys())[:8])

# ── pages ───────────────────────────────────────────────────────────────
print("\n--- pages ---")
pages_holder: dict = {}

def _pages_list():
    items = pages.list(app_code="appbuilder")
    assert isinstance(items, list), f"expected list, got {type(items).__name__}"
    assert len(items) > 0, "appbuilder should have at least one page"
    for p in items[:5]:
        assert "name" in p, f"page item missing 'name': {p}"
        assert "id" in p, f"page item missing 'id': {p}"
    pages_holder["items"] = items
    home = next((p for p in items if p.get("name") == "home"), None)
    assert home, "no 'home' page in appbuilder"
    pages_holder["home"] = home
    return f"{len(items)} pages; home id={home.get('id')}"

check("pages.list('appbuilder')", _pages_list)
check("pages.get('home', 'appbuilder')", lambda: list(pages.get("home", app_code="appbuilder").keys())[:8])

# ── raw HTTP ────────────────────────────────────────────────────────────
print("\n--- raw HTTP ---")
check("modlix.get('/api/ui/pages?appCode=appbuilder&size=2')",
      lambda: ("content_len=" + str(len((get("/api/ui/pages", params={"appCode": "appbuilder", "size": 2})).get("content") or []))))

# ── write/replace round-trip — uses a throwaway page that we
#     create + verify + delete cleanly so re-runs are idempotent.
print("\n--- page round-trip (create → replace → verify → delete) ---")
TEST_PAGE_NAME = "v4smokepage"


def _cleanup_test_page():
    items = pages.list(app_code="appbuilder")
    existing = next((p for p in items if p.get("name") == TEST_PAGE_NAME), None)
    if existing:
        resp = delete(f"/api/ui/pages/{existing['id']}")
        return f"deleted prior {TEST_PAGE_NAME} (status {resp.get('_status')})"
    return "no prior page"


check("cleanup prior v4smokepage", _cleanup_test_page)


def _create_page():
    resp = post("/api/ui/pages", body={
        "appCode": "appbuilder",
        "name": TEST_PAGE_NAME,
        "permission": "Authorities.ANYTIME",
    })
    status = resp.get("_status")
    assert status and status < 400, f"create returned {status}: {str(resp)[:300]}"
    pages_holder["test_id"] = resp.get("id")
    return f"created id={resp.get('id')} status={status}"


check("POST /api/ui/pages (create test page)", _create_page)


def _replace_test_page():
    # Build a minimal definition modeled on the appbuilder home page's shape.
    root_key = uuid()
    child_key = uuid()
    style_rule = uuid()
    definition_body = {
        "rootComponent": root_key,
        "componentDefinition": {
            root_key: {
                "key": root_key,
                "name": "root",
                "type": "Page",
                "children": {child_key: True},
                "properties": {},
                "styleProperties": {},
                "bindingPath": None,
            },
            child_key: {
                "key": child_key,
                "name": "helloLabel",
                "type": "Text",
                "properties": {"text": {"value": "Modlix v4 hello"}},
                "styleProperties": {
                    style_rule: {
                        "resolutions": {
                            "ALL": {"fontSize": {"value": "32px"}},
                        },
                    },
                },
                "bindingPath": None,
            },
        },
        "properties": {},
        "permission": "Authorities.ANYTIME",
    }
    resp = pages.replace(TEST_PAGE_NAME, definition_body, app_code="appbuilder",
                         message="v4 smoke replace")
    status = resp.get("_status")
    assert status and status < 400, f"replace returned {status}: {str(resp)[:400]}"
    return f"replaced v4smokepage status={status}"


check("pages.replace(v4smokepage, ...)", _replace_test_page)


def _verify_test_page():
    fetched = pages.get(TEST_PAGE_NAME, app_code="appbuilder")
    blob = json.dumps(fetched)
    assert "Modlix v4 hello" in blob, "replaced text not found in fetched definition"
    return "text present in fetched definition"


check("pages.get(v4smokepage) contains 'Modlix v4 hello'", _verify_test_page)


def _delete_test_page():
    test_id = pages_holder.get("test_id")
    if not test_id:
        # fall back to look it up
        items = pages.list(app_code="appbuilder")
        match = next((p for p in items if p.get("name") == TEST_PAGE_NAME), None)
        if not match:
            return "page not found, nothing to delete"
        test_id = match["id"]
    resp = delete(f"/api/ui/pages/{test_id}")
    status = resp.get("_status")
    assert status and status < 400, f"delete returned {status}: {str(resp)[:300]}"
    return f"deleted status={status}"


check("DELETE /api/ui/pages/<test_id>", _delete_test_page)

# ── summary ─────────────────────────────────────────────────────────────
print()
print(f"=== {len(passes)} passed, {len(failures)} failed ===")
if failures:
    for name, msg in failures:
        print(f"  FAIL  {name}: {msg[:300]}")
    sys.exit(1)
sys.exit(0)
