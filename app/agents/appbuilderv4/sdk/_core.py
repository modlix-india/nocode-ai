"""Core of the `modlix` SDK that's imported inside the code_run sandbox.

Auth + gateway config are read from environment variables that the
code_run tool sets immediately before spawning the subprocess. The SDK is
intentionally THIN — wraps HTTP, exposes the component catalog, and
provides 5 platform-aware namespaces (`catalog`, `pages`, `apps`). For
anything else (storages, functions, schemas) the agent calls
`modlix.post/get/put/delete` directly with the right path.

This keeps the surface small enough that the agent learns it by reading
the SDK source, rather than by reading a thick reference doc.
"""

from __future__ import annotations

import json
import os
import sys
import uuid as _uuid_lib
from dataclasses import dataclass
from typing import Any

import requests


# ── config (populated from env vars set by code_run) ────────────────────


@dataclass
class _Config:
    gateway_url: str
    auth_token: str
    app_code: str
    client_code: str
    forwarded_host: str
    forwarded_port: str
    catalog_url: str

    @classmethod
    def from_env(cls) -> "_Config":
        return cls(
            gateway_url=os.environ.get("MODLIX_GATEWAY_URL", "").rstrip("/"),
            auth_token=os.environ.get("MODLIX_AUTH_TOKEN", ""),
            app_code=os.environ.get("MODLIX_APP_CODE", ""),
            client_code=os.environ.get("MODLIX_CLIENT_CODE", ""),
            forwarded_host=os.environ.get("MODLIX_FORWARDED_HOST", "localhost:8080"),
            forwarded_port=os.environ.get("MODLIX_FORWARDED_PORT", "8080"),
            catalog_url=os.environ.get("MODLIX_CATALOG_URL", ""),
        )


config = _Config.from_env()


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build the auth + routing headers every gateway call needs."""
    h = {
        "Authorization": f"Bearer {config.auth_token}",
        "clientCode": config.client_code,
        "appCode": config.app_code,
        "X-Forwarded-Host": config.forwarded_host,
        "X-Forwarded-Port": config.forwarded_port,
    }
    if extra:
        h.update(extra)
    return h


def _url(path: str) -> str:
    if path.startswith(("http://", "https://")):
        return path
    if not path.startswith("/"):
        path = "/" + path
    if not config.gateway_url:
        raise RuntimeError("MODLIX_GATEWAY_URL not set — code_run env not initialised")
    return config.gateway_url + path


def _try_refresh_token() -> bool:
    """Re-authenticate using credentials in $MODLIX_CREDS_PATH (defaults
    to ~/.cfa-creds). Updates `config.auth_token` on success. Returns
    True if refreshed, False otherwise.

    The agent's JWT TTL is ~30 minutes — a single long-running session
    can outlive it. Sandbox subprocesses get the JWT from env vars at
    launch and have no other way to refresh, so we keep credentials
    reachable for an in-sandbox re-auth.
    """
    import json
    creds_path = os.environ.get("MODLIX_CREDS_PATH") or os.path.expanduser("~/.cfa-creds")
    if not os.path.exists(creds_path):
        return False
    try:
        creds = json.loads(open(creds_path).read())
    except Exception:  # noqa: BLE001
        return False
    body = {
        "userName": creds.get("username"), "password": creds.get("password"),
        "identifierType": creds.get("identifierType", "EMAIL_ID"),
        "loggedInClientCode": creds.get("clientCode", "SYSTEM"),
    }
    headers = {
        "Content-Type": "application/json",
        "X-Forwarded-Host": config.forwarded_host, "X-Forwarded-Port": config.forwarded_port,
        "clientCode": creds.get("clientCode", "SYSTEM"),
        "appCode": creds.get("appCode", "appbuilder"),
    }
    try:
        r = requests.post(f"{config.gateway_url}/api/security/authenticate",
                          json=body, headers=headers, timeout=15)
    except requests.RequestException:
        return False
    if r.status_code != 200:
        return False
    try:
        tok = r.json().get("accessToken")
    except ValueError:
        return False
    if not tok:
        return False
    config.auth_token = tok
    # Also persist to JWT_PATH so the parent loop wrapper sees the fresh
    # token if it checks the cache file.
    try:
        jwt_path = os.path.expanduser("~/.cfa-jwt")
        with open(jwt_path, "w") as fh:
            fh.write(tok)
        os.chmod(jwt_path, 0o600)
    except OSError:
        pass
    return True


def _request(method: str, path: str, body: Any = None, params: dict | None = None,
             headers: dict | None = None, timeout: float = 30.0,
             _retried: bool = False) -> dict:
    """One sync HTTP call. Returns parsed JSON when the response is JSON,
    otherwise a dict with `{"_status": int, "_text": str}`. Raises
    `RuntimeError` on transport failure or 5xx.

    On 401, auto-refreshes the JWT (via credentials in MODLIX_CREDS_PATH
    or ~/.cfa-creds) ONCE and retries. Long-running sandbox processes
    outlive the platform's ~30-min JWT TTL otherwise.
    """
    url = _url(path)
    req_headers = _headers(headers)
    json_body = None
    data = None
    if isinstance(body, (dict, list)):
        json_body = body
    elif body is not None:
        data = body
    try:
        resp = requests.request(
            method, url, params=params, headers=req_headers,
            json=json_body, data=data, timeout=timeout,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"{method} {path} transport error: {type(e).__name__}: {e}") from e

    # JWT auto-refresh on 401 — ONE retry only.
    if resp.status_code == 401 and not _retried and _try_refresh_token():
        return _request(method, path, body=body, params=params,
                        headers=headers, timeout=timeout, _retried=True)
    if resp.status_code >= 500:
        raise RuntimeError(f"{method} {path} → {resp.status_code}: {resp.text[:600]}")
    out: Any
    try:
        parsed = resp.json()
    except ValueError:
        return {"_status": resp.status_code, "_text": resp.text}
    if isinstance(parsed, dict):
        parsed["_status"] = resp.status_code
        return parsed
    if isinstance(parsed, list):
        return {"_status": resp.status_code, "items": parsed}
    # Bool / int / null / str — wrap so the caller always gets a dict.
    return {"_status": resp.status_code, "value": parsed}


# ── public HTTP helpers ──────────────────────────────────────────────────


def get(path: str, params: dict | None = None, headers: dict | None = None) -> dict:
    """GET a gateway path. Auth headers are added automatically."""
    return _request("GET", path, params=params, headers=headers)


def post(path: str, body: Any = None, params: dict | None = None, headers: dict | None = None) -> dict:
    """POST a gateway path. `body` is JSON-encoded if it's a dict/list."""
    return _request("POST", path, body=body, params=params, headers=headers)


def put(path: str, body: Any = None, params: dict | None = None, headers: dict | None = None) -> dict:
    """PUT a gateway path."""
    return _request("PUT", path, body=body, params=params, headers=headers)


def delete(path: str, params: dict | None = None, headers: dict | None = None) -> dict:
    """DELETE a gateway path."""
    return _request("DELETE", path, params=params, headers=headers)


# ── catalog ──────────────────────────────────────────────────────────────


class _Catalog:
    """Component-type catalog. Lazy-loaded from MODLIX_CATALOG_URL on
    first call. Don't worry about cost — it's one cached HTTP fetch per
    code_run invocation, ~200ms one-time."""

    def __init__(self) -> None:
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        """Fetch + cache the catalog. The CDN JSON wraps the component dict
        under a `components` key alongside metadata (version, generatedAt);
        we return just the components dict so list_types/get_schema see one
        flat name→schema mapping."""
        if self._data is not None:
            return self._data
        if not config.catalog_url:
            raise RuntimeError("MODLIX_CATALOG_URL not set — catalog cannot be loaded")
        try:
            resp = requests.get(config.catalog_url, timeout=10.0)
        except requests.RequestException as e:
            raise RuntimeError(f"catalog fetch failed: {e}") from e
        if resp.status_code >= 400:
            raise RuntimeError(f"catalog HTTP {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"catalog payload must be a dict, got {type(payload).__name__}")
        components = payload.get("components")
        if not isinstance(components, dict):
            # Older catalog shape: payload IS the component dict.
            components = payload
        self._data = components
        return components

    def list_types(self) -> list[str]:
        """Return every component-type name available on this Modlix instance."""
        return sorted(self._load().keys())

    def get_schema(self, name: str) -> dict[str, Any]:
        """Full schema (properties, styleProperties, events, allowed children)
        for one component type. Raises KeyError when the name is unknown."""
        data = self._load()
        if name not in data:
            raise KeyError(f"unknown component type {name!r}; call catalog.list_types() for the full list")
        return data[name]

    def search(self, keyword: str) -> list[str]:
        """Return type names whose name or summary contains `keyword`."""
        kw = keyword.lower()
        data = self._load()
        hits: list[str] = []
        for name, schema in data.items():
            if kw in name.lower():
                hits.append(name)
                continue
            summary = (schema.get("description") or schema.get("summary") or "")
            if isinstance(summary, str) and kw in summary.lower():
                hits.append(name)
        return sorted(hits)


catalog = _Catalog()


# ── pages ────────────────────────────────────────────────────────────────


def _unwrap_page(resp: dict) -> list[dict]:
    """Spring REST endpoints return `{content: [...], totalElements, ...}`.
    Return just the list of items so the agent doesn't have to dig into
    `.content`. Raises `RuntimeError` when the response is an error envelope
    (exceptionId / 4xx) — the agent gets a clean traceback instead of an
    error dict masquerading as a single-item list. Use the low-level
    `modlix.get(...)` if you need the wrapper or want to handle errors."""
    if not isinstance(resp, dict):
        return []
    if isinstance(resp.get("content"), list):
        return resp["content"]
    if isinstance(resp.get("items"), list):
        return resp["items"]
    status = resp.get("_status")
    if isinstance(status, int) and status >= 400:
        msg = resp.get("message") or resp.get("debugMessage") or resp.get("_text") or str(resp)[:300]
        raise RuntimeError(f"server returned {status}: {msg}")
    if "exceptionId" in resp:
        msg = resp.get("message") or resp.get("debugMessage") or str(resp)[:300]
        raise RuntimeError(f"server error: {msg}")
    return []


class _Pages:
    """Page READS + atomic REPLACE via `/api/ui/pages`. The platform's
    page identity is a Mongo id (returned in `id`), not the human-
    readable `name`. Reads go through list-then-detail-by-id because
    the platform exposes no name-based read endpoint.

    Page CREATE / DELETE are intentionally NOT exposed here — like app
    create, they have multi-step quirks (UI doc + dependencies). See the
    persona for the recipe; use `modlix.post('/api/ui/pages', ...)` and
    `modlix.delete('/api/ui/pages/{id}')` directly.
    """

    def list(self, app_code: str | None = None, size: int = 200) -> list[dict]:
        """Return a plain list of page summaries for the given app. Each
        item has `id`, `name`, `applicationName`, `clientCode`, `version`,
        etc. (NOT the full componentDefinition — call `get(name)` for that.)

        NOTE: the platform's `pageType` query param filters every value to
        zero matches on this build — we deliberately do NOT pass it.
        """
        ac = app_code or config.app_code
        resp = get("/api/ui/pages", params={"appCode": ac, "size": size})
        return _unwrap_page(resp)

    def get(self, name: str, app_code: str | None = None) -> dict:
        """Resolve `name` → `id` via list, then fetch the full page detail
        (componentDefinition + properties + event functions + versions).
        Raises KeyError when no page in the app has the given name."""
        ac = app_code or config.app_code
        items = self.list(app_code=ac)
        match = next((p for p in items if p.get("name") == name), None)
        if match is None:
            raise KeyError(f"no page named {name!r} in app {ac!r} "
                           f"(available: {[p.get('name') for p in items][:10]})")
        return get(f"/api/ui/pages/{match['id']}")

    def replace(self, name: str, definition: dict, app_code: str | None = None,
                message: str = "") -> dict:
        """Atomic replace of a page's componentDefinition + properties.

        Resolves `name` → `id` via list, FETCHES the existing page detail
        (preserves `id`, `version`, `clientCode`, `createdAt/By` etc.),
        merges your `definition` into that detail, then PUTs the full
        document. Without the merge the platform's PUT returns 200 but
        SILENTLY discards your changes (no version, no id → no-op).

        Pass `definition` as a dict containing ONLY the fields you want
        to change (typically `rootComponent`, `componentDefinition`,
        `properties`, `eventFunctions`). Untouched fields are preserved
        from the server's current copy. The page must already exist;
        create one via `modlix.post('/api/ui/pages', {...})` first."""
        ac = app_code or config.app_code
        items = self.list(app_code=ac)
        match = next((p for p in items if p.get("name") == name), None)
        if match is None:
            raise KeyError(f"no page named {name!r} in app {ac!r}; create it first")
        # Fetch current full detail (id, version, clientCode, etc.).
        current = get(f"/api/ui/pages/{match['id']}")
        if not isinstance(current, dict) or "_status" in current and int(current.get("_status", 200)) >= 400:
            raise RuntimeError(f"could not fetch current page detail: {current}")
        # Merge: caller's `definition` overrides current fields.
        merged = {k: v for k, v in current.items() if not k.startswith("_")}
        merged.update(definition)
        merged["name"] = name
        merged["appCode"] = ac
        if message:
            merged["message"] = message
        # Pre-PUT shape validation — catches the styleProperty UUID bloat
        # that breaks clones (and other shape mistakes) BEFORE the platform
        # silently persists them.
        from app.agents.appbuilderv4.sdk._validators import (
            ModlixShapeError, format_issues, validate_page,
        )
        issues = validate_page(merged)
        if issues:
            raise ModlixShapeError(format_issues(
                f"page {name!r}",
                issues,
                hint=(
                    "Use `modlix.components.set_style(component, {...css...})` "
                    "to write component style (replaces all UUID entries with "
                    "ONE canonical entry), or `modlix.components.merge_style"
                    "(component, {...})` to update a few keys without erasing "
                    "the rest. Pass the COMPONENT dict (e.g. `cd[component_key]`), "
                    "NOT the whole componentDefinition map."
                ),
            ))
        return put(f"/api/ui/pages/{match['id']}", body=merged)


pages = _Pages()


# ── apps ─────────────────────────────────────────────────────────────────


class _Apps:
    """App READS via `/api/ui/applications`. Writes are intentionally NOT
    exposed here — creating/updating/deleting an app is a multi-step
    platform recipe (security row + UI doc) that we don't want to hide
    behind a single SDK call. See the persona for the recipe; use
    `modlix.post('/api/security/applications', ...)` plus
    `modlix.post('/api/ui/applications', ...)` directly.
    """

    def list(self) -> list[dict]:
        """Return every app registered in the security DB (the directory of
        record). Each item has `id`, `appCode`, `appName`, `appType`,
        `appAccessType`. Returns `[]` when no apps exist.

        Hits `/api/security/applications`; the `/api/ui/applications` path
        is NOT a directory — it requires an appCode and returns the
        per-app override doc."""
        return _unwrap_page(get("/api/security/applications", params={"size": 200}))

    def get_security(self, app_code: str | None = None) -> dict:
        """Fetch one app's security row (the appCode + appType + access
        rules). Used to confirm an app exists before you try to load its
        UI override doc."""
        ac = app_code or config.app_code
        items = self.list()
        match = next((a for a in items if a.get("appCode") == ac), None)
        if match is None:
            raise KeyError(f"no app {ac!r} in security directory")
        return match

    def get_ui(self, app_code: str | None = None) -> dict:
        """Fetch one app's UI-side override doc (properties, languages,
        translations). The platform's GET-by-id endpoint requires the
        Mongo `id`, not the appCode — so we list with appCode filter and
        return the first match.

        Raises KeyError when the app has no UI override doc yet (the
        security row may exist but the UI doc isn't auto-created — that's
        a separate POST /api/ui/applications)."""
        ac = app_code or config.app_code
        resp = get("/api/ui/applications", params={"appCode": ac, "size": 5})
        items = _unwrap_page(resp)
        match = next((a for a in items if a.get("appCode") == ac), items[0] if items else None)
        if match is None:
            raise KeyError(f"no UI app doc for {ac!r}; the security row may "
                           "exist but the UI doc has never been written")
        return match


apps = _Apps()


# ── utility ──────────────────────────────────────────────────────────────


def uuid() -> str:
    """Return a fresh uuid4 string. Use as the rule key in styleProperties."""
    return str(_uuid_lib.uuid4())
