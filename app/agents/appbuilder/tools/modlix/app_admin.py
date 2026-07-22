"""App-admin CRUD — applications, themes, styles, uri_paths.

Ports modlix-mcp/modlix_mcp/tools/{apps,themes,styles,uri_paths}.py — 22 tools
total (apps:7, themes:5, styles:5, uri_paths:5).

  - **apps**         → /api/ui/applications (ui-override doc) + /api/security/applications (directory)
  - **themes**       → /api/ui/themes (per-breakpoint CSS variable maps)
  - **styles**       → /api/ui/styles (raw CSS app-wide)
  - **uri_paths**    → /api/ui/uripaths (REST routes that invoke Kirun functions)

Every list-then-fetch helper resolves to the entity's id via the standard
`?page=0&size=1&appCode=<ac>&name=<n>` pattern, then fetches the detail doc.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

from . import _conventions as c


# Shared param-description constants.
_DESC_APP_CODE = "appCode; defaults to session"
_DESC_CLIENT_CODE = "clientCode; defaults to session"
_DESC_COMMIT_MSG = "Commit message"
_DESC_SIZE = "Max rows"

# Default commit messages stamped on create_* / update_* tool writes.
_DEFAULT_CREATE_MESSAGE = "Created via CFA"
_DEFAULT_UPDATE_MESSAGE = "Updated via CFA"

# Common validation error messages.
_ERR_NAME_REQUIRED = "`name` is required"


def _client_and_headers(context: dict[str, Any]) -> tuple[Any, dict[str, str]]:
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context.get("headers") or {}


def _resolve_app_code(params: dict[str, Any], context: dict[str, Any]) -> str:
    return (params.get("app_code") or context.get("app_code") or "").strip()


def _resolve_client_code(params: dict[str, Any], context: dict[str, Any]) -> str:
    return params.get("client_code") or context.get("client_code", "") or ""


def _page_size(params: dict[str, Any], default: int = 100, cap: int = 1000) -> int:
    try:
        return max(1, min(int(params.get("size") or default), cap))
    except (TypeError, ValueError):
        return default


async def _find_by_name(
    client: Any, headers: dict[str, str], api: str, app_code: str, name: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """List-with-filter then GET detail by id. Returns (doc, error)."""
    r = await client.get(api, headers=headers, params={"page": 0, "size": 1, "appCode": app_code, "name": name})
    if not r.success:
        return None, r.error
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    if not content:
        return None, f"not found in app '{app_code}'."
    detail = await client.get(f"{api}/{content[0].get('id')}", headers=headers)
    if not detail.success:
        return None, detail.error
    return (detail.data if isinstance(detail.data, dict) else {}), None


def _err_app_code() -> ToolResult:
    return ToolResult(success=False, error="`app_code` is required (set in context or pass explicitly).")


# ═════════════════════════════════════════════════════════════════════════
#  APPLICATIONS (7 tools)
# ═════════════════════════════════════════════════════════════════════════
#
# Two endpoints:
#   /api/ui/applications      — ui-override doc per app (POST/PUT/DELETE)
#   /api/security/applications — directory listing visible via ClientHierarchy

_APPS_API = "/api/ui/applications"
_SECURITY_APPS_API = "/api/security/applications"


async def _execute_list_apps(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _client_and_headers(context)
    p: dict[str, Any] = {"page": max(0, int(params.get("page") or 0)), "size": _page_size(params, 20, 500)}
    # Exact-match filter takes precedence — appCode is unique on the server side,
    # so when the caller knows it there's no reason to paginate.
    if params.get("app_code"):
        p["appCode"] = params["app_code"]
    elif params.get("name_filter"):
        p["name"] = params["name_filter"]
    r = await client.get(_SECURITY_APPS_API, headers=headers, params=p)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    rows = [{
        "id": a.get("id"), "appCode": a.get("appCode"),
        "name": a.get("appName") or a.get("name"),
        "appType": a.get("appType"), "appAccessType": a.get("appAccessType"),
    } for a in content]
    total = (r.data or {}).get("totalElements", len(rows)) if isinstance(r.data, dict) else len(rows)
    return ToolResult(success=True, summary=f"Found {len(rows)} apps (total={total}):\n{json.dumps(rows, indent=2, default=str)}")


list_apps_tool = ToolDefinition(
    name="list_apps",
    description="""List applications visible to the caller. Returns appCode + display name + type + accessType.

Choose your lookup path BEFORE calling — most agent waste here is asking for 50 rows then scanning the response:

- **If you already know the appCode** → call `get_app(app_code="…")` instead. Direct fetch, one row, no scanning. `list_apps` is for discovery, not for verifying an app you just created.
- **If you have the appCode but want a quick "does it exist?" check** → pass `app_code` here for an exact-match lookup (returns 0 or 1 row).
- **If you only have a partial display name** (e.g. user said "Stalin's Victim" but you don't know the slug) → pass `name_filter`. This is a substring match on the DISPLAY name (`APP_NAME`), not on appCode — fuzzy hits are expected.
- **Browsing what's available** → no filter; lower the `size` (default 20) if you only need a sample.

Don't paginate to find a specific app you already named in this session. Save its appCode in your reasoning instead.""",
    parameters=[
        ToolParameter(name="app_code", type="string", required=False, description="Exact appCode to look up (unique key). Returns 0 or 1 row — use this when you already know the appCode."),
        ToolParameter(name="name_filter", type="string", required=False, description="Substring match on the DISPLAY name (APP_NAME). Fuzzy — returns every match."),
        ToolParameter(name="page", type="integer", required=False, default=0, description="Zero-indexed page"),
        ToolParameter(name="size", type="integer", required=False, default=20, description=_DESC_SIZE),
    ],
    execute=_execute_list_apps,
)


async def _execute_get_app(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    cc = _resolve_client_code(params, context)
    client, headers = _client_and_headers(context)
    r = await client.get(f"{_APPS_API}/{ac}/index", headers=headers, params={"clientCode": cc} if cc else None)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Application '{ac}':\n{json.dumps(r.data, indent=2, default=str)}")


get_app_tool = ToolDefinition(
    name="get_app",
    description="Read an application's full definition (properties, languages, theme, version) by appCode.",
    parameters=[
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description=_DESC_CLIENT_CODE),
    ],
    execute=_execute_get_app,
)


_APP_TYPES = ("APP", "SITE", "POSTER")
_APP_ACCESS_TYPES = ("OWN", "ANY", "EXPLICIT")


def _validate_create_app_params(params: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """Validate + normalize create_app params. Returns (error_message, normalized_params).

    Platform constraint (ApplicationService.java:71-76): name MUST equal appCode,
    and appCode MUST be alphabet-only (no digits, no separators). We enforce both
    here so the agent gets a clear error before hitting the gateway.
    """
    app_code = (params.get("app_code") or "").strip()
    if not app_code:
        return "`app_code` is required", None
    if not app_code.isalpha():
        return (
            f"`app_code` must be alphabet-only (no digits, no underscores, no dashes); got {app_code!r}",
            None,
        )
    # Platform enforces name == appCode. The `name` param is accepted for backwards
    # compatibility but is always overridden — never silently accept a different value.
    name = app_code
    app_type = (params.get("app_type") or "SITE").upper()
    if app_type not in _APP_TYPES:
        return f"app_type must be {'|'.join(_APP_TYPES)}, got {app_type!r}", None
    app_access_type = (params.get("app_access_type") or params.get("access_type") or "OWN").upper()
    if app_access_type not in _APP_ACCESS_TYPES:
        return f"app_access_type must be {'|'.join(_APP_ACCESS_TYPES)}, got {app_access_type!r}", None
    return None, {
        "app_code": app_code, "name": name,
        "app_type": app_type, "app_access_type": app_access_type,
    }


def _coerce_to_lang_map(value: Any) -> dict[str, dict[str, str]] | None:
    """Normalize languages input to `Map<String, Map<String, String>>`.

    Accepts either a list of language codes (`["en", "fr"]`) — coerced into
    `{"en": {}, "fr": {}}` — or an already-shaped dict; returns None for anything
    else so the field is dropped instead of crashing the platform's Jackson parse.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        out: dict[str, dict[str, str]] = {}
        for item in value:
            if isinstance(item, str) and item.strip():
                out[item.strip()] = {}
        return out or None
    return None


async def _upsert_ui_app(
    params: dict[str, Any], context: dict[str, Any], app_code: str, name: str, sec_id: Any,
) -> ToolResult:
    """Create the UI-side application override doc. ALWAYS called after the
    security registration, with at minimum an empty `properties: {}`.

    Why this is mandatory (not optional) — the UI service refuses every
    /api/ui/applications and /api/ui/pages READ for an app whose UI doc
    doesn't exist (returns 403 even though the security row is there). The
    agent then can't list its own pages or update the app definition. The
    Modlix UI button calls only the security endpoint and relies on the IDE
    to create the UI doc on first save — that path doesn't exist for the
    CFA, so it owns both writes.

    Optional UI extras (properties / languages / translations / default_page)
    layer on top of the empty defaults. `default_page` is a convenience that
    sets `properties.defaultPage` so the app becomes browser-reachable as
    soon as the matching page exists.
    """
    properties = dict(params.get("properties") or {})
    default_page = (params.get("default_page") or "").strip()
    if default_page and "defaultPage" not in properties:
        properties["defaultPage"] = default_page

    # Platform shell (RenderEngineContainer + App.tsx) crashes on missing
    # `fontPacks` / `iconPacks` — `Cannot read properties of undefined
    # (reading 'fontPacks')` from src/App/App.tsx:204. Bootstrap an empty
    # map for both so the shell renders. If the agent later wants real
    # font packs, it can patch via update_app.
    properties.setdefault("fontPacks", {})
    properties.setdefault("iconPacks", {})

    # Platform enforces name == appCode (ApplicationService.java:71-76). Override
    # whatever caller passed in `name` — never let a different value reach the POST.
    ui_body: dict[str, Any] = {
        "appCode": app_code, "name": app_code,
        "clientCode": _resolve_client_code(params, context),
        "message": params.get("message") or _DEFAULT_CREATE_MESSAGE,
        "properties": properties,
    }
    # `languages` / `translations` are Map<String, Map<String, String>> on the
    # platform Application bean. List-shaped values (e.g. ["en"]) crash Jackson
    # with "Failed to read HTTP message". Coerce list -> {code: {}} per item;
    # silently drop anything else that isn't already a dict.
    lang = _coerce_to_lang_map(params.get("languages"))
    if lang:
        ui_body["languages"] = lang
    tr = params.get("translations")
    if isinstance(tr, dict):
        ui_body["translations"] = tr

    client, headers = _client_and_headers(context)
    ui_resp = await client.post(_APPS_API, headers=headers, json=ui_body)
    if not ui_resp.success:
        # 409 means a UI doc with this appCode already exists from a prior partial
        # create — fine, the app is now fully present in both layers. Reads will
        # succeed against the existing doc. The caller can layer additional
        # properties via update_app if needed.
        err_text = str(ui_resp.error or "")
        if "409" in err_text or "already exists" in err_text.lower():
            return ToolResult(
                success=True,
                summary=(
                    f"Created security app '{app_code}' (id={sec_id}); UI override "
                    f"doc was already present from a prior run (409). The app is "
                    f"fully usable — reads will succeed against the existing UI doc. "
                    f"If you need to tweak properties/languages/translations, call "
                    f"update_app."
                ),
            )
        return ToolResult(
            success=True,
            summary=(
                f"Created security app '{app_code}' (id={sec_id}), but the UI "
                f"override write failed: {ui_resp.error}. The app is "
                f"PARTIALLY CREATED — listing pages or visiting the app URL "
                f"will 403 until you retry the UI write via update_app."
            ),
        )
    next_step = (
        ""
        if default_page
        else " Next: create the first page, then call update_app to set defaultPage so the app becomes browser-reachable."
    )
    return ToolResult(
        success=True,
        summary=f"Created application '{app_code}' (security id={sec_id}, ui id={(ui_resp.data or {}).get('id', '?')}).{next_step}",
    )


async def _find_security_app_by_code(client: Any, headers: dict, app_code: str) -> dict | None:
    """Return the existing ACTIVE security_app row for app_code, or None.

    Lets create_app be idempotent: a re-invocation with the same appCode skips
    the POST (which would 500 on duplicate-key — the platform doesn't map
    IntegrityConstraintViolationException to 409) and proceeds straight to
    step 2 (UI doc creation).

    DELETE on the security row soft-archives it (status=ARCHIVED) — the row
    stays findable but is dead. Treat those as "not found" so the create path
    can mint a fresh row. The platform's POST will fail on the unique-key if
    it disagrees; that surfaces as a clear duplicate error rather than the
    silent "row exists but it's a zombie" trap.
    """
    r = await client.get(_SECURITY_APPS_API, headers=headers, params={"appCode": app_code, "size": 5})
    if not r.success or not isinstance(r.data, dict):
        return None
    content = r.data.get("content") or []
    for row in content:
        if row.get("appCode") != app_code:
            continue
        if (row.get("status") or "").upper() == "ARCHIVED":
            continue
        return row
    return None


async def _execute_create_app(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    err, p = _validate_create_app_params(params)
    if err or p is None:
        return ToolResult(success=False, error=err or "invalid params")

    client, headers = _client_and_headers(context)

    # Step 0: existence check. Without this, a retry after a transient 500
    # (e.g. duplicate-key returned as 500 by the platform's ControllerAdvice)
    # makes the LLM think the create failed and pick a new appCode — leaving
    # orphan rows behind. A bench run created 4 such orphans in one session.
    existing = await _find_security_app_by_code(client, headers, p["app_code"])
    if existing:
        sec_id = existing.get("id", "?")
        ui_result = await _upsert_ui_app(params, context, p["app_code"], p["name"], sec_id)
        # Prepend an explicit "already existed" marker so the LLM doesn't
        # try to pick a different appCode on the next turn.
        return ToolResult(
            success=ui_result.success,
            summary=(
                f"Security app '{p['app_code']}' already existed (id={sec_id}); "
                f"skipped re-create. {ui_result.summary or ''}".strip()
            ),
            error=ui_result.error,
            data=ui_result.data,
        )

    # Step 1: register the app in the SECURITY service. The Modlix UI's "Create
    # app" button only calls this endpoint; the UI-side override doc gets
    # written separately when the IDE first saves the app. Without this row,
    # the AuthorizationWebFilter on /api/ui/applications and /api/ui/pages
    # rejects every write with 403 because the security service has no
    # record of the app.
    sec_body: dict[str, Any] = {
        "appCode": p["app_code"],
        "appName": p["name"],
        "appType": p["app_type"],
        "appAccessType": p["app_access_type"],
    }
    if params.get("thumb_url"):
        sec_body["thumbUrl"] = params["thumb_url"]
    sec_resp = await client.post(_SECURITY_APPS_API, headers=headers, json=sec_body)
    if not sec_resp.success:
        # 500 on duplicate-key is a platform bug (should be 409). Detect it,
        # surface a clear "already exists" message instead of a generic 500,
        # and DO NOT retry with a different appCode.
        err_text = (sec_resp.error or "").lower()
        if "duplicate" in err_text and p["app_code"].lower() in err_text:
            return ToolResult(
                success=False,
                error=(
                    f"App '{p['app_code']}' already exists (security duplicate-key). "
                    f"DO NOT retry with a different appCode — call create_app again "
                    f"with the SAME appCode to fall into the idempotent path that "
                    f"resumes from step 2 (UI doc upsert)."
                ),
            )
        return ToolResult(success=False, error=f"Security app registration failed: {sec_resp.error}")

    # Step 2: write the UI-side override doc. ALWAYS — without it the UI
    # service rejects every read for this app (including listing its own
    # pages) and the app stays invisible.
    sec_id = (sec_resp.data or {}).get("id", "?")
    return await _upsert_ui_app(params, context, p["app_code"], p["name"], sec_id)


create_app_tool = ToolDefinition(
    name="create_app",
    description="""Create a new application end-to-end. Writes BOTH the security registration AND the UI-side application override doc in one call — both are required for the app to be usable; the security row alone leaves the app invisible (every /api/ui/* read 403s).

Required: `app_code` — letters only (no digits, no underscores, no dashes — the platform enforces `onlyAlphabetAllowed`).

**Important platform rule:** the UI document's `name` MUST equal `app_code` exactly. The `name` parameter is accepted for compatibility but always overridden to match `app_code` — you cannot give the app a separate display name through this tool. If you want a display label, set it via translations later.

Optional but important:
- `default_page` — the page name to set as `properties.defaultPage`. The app needs a defaultPage to be browser-reachable; pass this if you already know which page will be the landing one. Otherwise, create your first page next, then call `update_app` (or `set_app_page_reference`) to set it.
- `app_type` — APP (default-routing) | SITE (the common one, default) | POSTER.
- `app_access_type` — OWN (only the creating client, default) | ANY (every tenant) | EXPLICIT (requires `grant_app_access` per client).

Two-step flow this tool runs:
1. POST /api/security/applications — registers the app in the security DB so subsequent writes pass authorization.
2. POST /api/ui/applications — writes the UI override doc (properties / languages / translations). Without this, GET on the app returns 403 even though the security row exists.

After this tool succeeds, the typical next moves are:
- `create_page(name="home", ...)` — your landing page.
- If you didn't pass `default_page`, `update_app(app_code=..., properties={"defaultPage": "home"})` — wires the app's entry route.""",
    parameters=[
        ToolParameter(name="app_code", type="string", description="Unique appCode (letters ONLY — no digits, no underscores, no dashes — used in URLs and security checks)"),
        ToolParameter(name="name", type="string", required=False, description="Accepted but always overridden to equal app_code (platform requirement). Pass app_code or omit."),
        ToolParameter(name="default_page", type="string", required=False, description="Page name to set as properties.defaultPage. Convenience — equivalent to setting it via `properties` later. The page itself doesn't need to exist yet; create it next and the routing links up."),
        ToolParameter(name="app_type", type="string", required=False, default="SITE", description="APP | SITE | POSTER. SITE is the usual choice for a customer-facing app."),
        ToolParameter(name="app_access_type", type="string", required=False, default="OWN", description="OWN | ANY | EXPLICIT. OWN restricts to the creating client."),
        ToolParameter(name="client_code", type="string", required=False, description="Owning clientCode for the UI doc (defaults to session)"),
        ToolParameter(name="properties", type="object", required=False, description="UI app properties: defaultPage, loginPage, shellPage, forbiddenPage, notFoundPage, signUp, etc. Top-level field names, NOT wrapped in {value:...}."),
        ToolParameter(name="languages", type="array", required=False, description="Supported locale codes, e.g. ['en','hi','ar']"),
        ToolParameter(name="translations", type="object", required=False, description="Translation map: {locale: {translationKey: translatedString}}"),
        ToolParameter(name="thumb_url", type="string", required=False, description="Thumbnail URL stored on the security app record"),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default=_DEFAULT_CREATE_MESSAGE),
    ],
    execute=_execute_create_app,
)


_VALID_SLOTS = ("defaultPage", "loginPage", "shellPage", "forbiddenPage")


def _validate_page_ref_params(params: dict[str, Any]) -> tuple[str | None, str, str, str]:
    """Return (error, slot, page_name, app_code). On error the other fields are empty."""
    slot = (params.get("slot") or "").strip()
    page_name = (params.get("page_name") or "").strip()
    if slot not in _VALID_SLOTS:
        return f"`slot` must be one of {_VALID_SLOTS}", "", "", ""
    if not page_name:
        return "`page_name` is required", "", "", ""
    return None, slot, page_name, ""


async def _lookup_ui_app_by_code(client: Any, headers: dict, app_code: str) -> tuple[dict | None, str | None]:
    """Find the UI app doc by exact appCode. Returns (row, error_text)."""
    listing = await client.get(_APPS_API, headers=headers, params={"appCode": app_code, "size": 1})
    if not listing.success:
        return None, listing.error
    rows = (listing.data or {}).get("content", []) if isinstance(listing.data, dict) else []
    match = next((a for a in rows if a.get("appCode") == app_code), None)
    return match, None


async def _create_ui_doc_with_page_ref(
    client: Any, headers: dict, params: dict[str, Any], context: dict[str, Any],
    app_code: str, slot: str, page_name: str, message: str,
) -> ToolResult:
    """Fallback when the UI doc is missing — create it with the slot pre-set."""
    ui_body: dict[str, Any] = {
        "appCode": app_code, "name": app_code,
        "clientCode": _resolve_client_code(params, context),
        "message": message,
        "properties": {slot: page_name},
    }
    create_resp = await client.post(_APPS_API, headers=headers, json=ui_body)
    if not create_resp.success:
        default_page_hint = page_name if slot == "defaultPage" else ""
        return ToolResult(
            success=False,
            error=(
                f"UI doc for '{app_code}' was missing AND the auto-create fallback "
                f"failed: {create_resp.error}. Run `create_app(app_code=\"{app_code}\", "
                f"name=\"{app_code}\", default_page=\"{default_page_hint}\") "
                f"first; it has clearer error reporting if the platform "
                f"authorization is the blocker."
            ),
        )
    return ToolResult(
        success=True,
        summary=f"Created missing UI doc for '{app_code}' with {slot}='{page_name}'.",
    )


async def _update_ui_doc_slot(
    client: Any, headers: dict, match: dict,
    app_code: str, slot: str, page_name: str, message: str,
) -> ToolResult:
    """UI doc exists — fetch detail, merge slot, PUT back."""
    detail = await client.get(f"{_APPS_API}/{match.get('id')}", headers=headers)
    if not detail.success:
        return ToolResult(success=False, error=detail.error)
    doc = detail.data if isinstance(detail.data, dict) else {}
    props = dict(doc.get("properties") or {})
    props[slot] = page_name
    doc["properties"] = props
    doc["message"] = message
    save = await client.put(f"{_APPS_API}/{match.get('id')}", headers=headers, json=doc)
    if not save.success:
        return ToolResult(success=False, error=save.error)
    return ToolResult(success=True, summary=f"Set {slot}='{page_name}' on '{app_code}'.")


async def _execute_set_app_page_reference(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    err, slot, page_name, _ = _validate_page_ref_params(params)
    if err:
        return ToolResult(success=False, error=err)
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()

    client, headers = _client_and_headers(context)
    msg = params.get("message") or _DEFAULT_UPDATE_MESSAGE

    # Look up the UI app doc by exact appCode. If not present, FALL BACK to
    # POST /api/ui/applications with the slot already set — the app may have
    # only a security row (orphan from a prior session or from the
    # security-only create flow). Without this fallback the agent gets stuck
    # in a "403 on update, no path forward" loop.
    match, lookup_err = await _lookup_ui_app_by_code(client, headers, ac)
    if lookup_err:
        return ToolResult(success=False, error=lookup_err)

    if not match:
        return await _create_ui_doc_with_page_ref(
            client, headers, params, context, ac, slot, page_name, msg,
        )
    return await _update_ui_doc_slot(client, headers, match, ac, slot, page_name, msg)


set_app_page_reference_tool = ToolDefinition(
    name="set_app_page_reference",
    description="Set one of the app's named-page references (defaultPage / loginPage / shellPage / forbiddenPage). The runtime reads these to route visitors. Pages must already exist.",
    parameters=[
        ToolParameter(name="slot", type="string", description=f"One of {_VALID_SLOTS}", enum=list(_VALID_SLOTS)),
        ToolParameter(name="page_name", type="string", description="Page name to point this slot at"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default="Updated app page reference via CFA"),
    ],
    execute=_execute_set_app_page_reference,
)


async def _execute_update_app(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    app_id = (params.get("app_id") or "").strip()
    if not app_id:
        return ToolResult(success=False, error="`app_id` is required (use list_apps to find it)")
    client, headers = _client_and_headers(context)
    existing = await client.get(f"{_APPS_API}/{app_id}", headers=headers)
    if not existing.success:
        return ToolResult(success=False, error=existing.error)
    body = existing.data if isinstance(existing.data, dict) else {}
    if params.get("name") is not None:
        body["name"] = params["name"]
    if params.get("properties") is not None:
        # App-level properties are stored as RAW values (e.g. loginPage: "login")
        # — not wrapped in {value: "..."} like component properties. Auto-unwrap
        # any over-wrapped scalars the agent passes so we don't end up with
        # properties.loginPage = {value: "login"}, which the platform reads as
        # a truthy dict and skips the login-page substitution.
        cleaned: dict[str, Any] = {}
        for k, v in params["properties"].items():
            if isinstance(v, dict) and set(v.keys()) == {"value"} and not isinstance(v["value"], (dict, list)):
                cleaned[k] = v["value"]
            else:
                cleaned[k] = v
        body.setdefault("properties", {}).update(cleaned)
    if params.get("languages") is not None:
        body["languages"] = params["languages"]
    if params.get("default_language") is not None:
        body["defaultLanguage"] = params["default_language"]
    if params.get("version") is not None:
        body["version"] = params["version"]
    body["message"] = params.get("message") or _DEFAULT_UPDATE_MESSAGE
    r = await client.put(f"{_APPS_API}/{app_id}", headers=headers, json=body)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Updated application id={app_id}.")


update_app_tool = ToolDefinition(
    name="update_app",
    description="Update an application's metadata. Requires the id from list_apps.",
    parameters=[
        ToolParameter(name="app_id", type="string", description="Application id (Mongo _id)"),
        ToolParameter(name="name", type="string", required=False, description="New display name"),
        ToolParameter(name="properties", type="object", required=False, description="Properties to merge into the app"),
        ToolParameter(name="languages", type="array", required=False, description="Supported languages"),
        ToolParameter(name="default_language", type="string", required=False, description="Default language code"),
        ToolParameter(name="version", type="integer", required=False, description="Expected version (optimistic lock)"),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default=_DEFAULT_UPDATE_MESSAGE),
    ],
    execute=_execute_update_app,
)


async def _execute_delete_app(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    app_id = (params.get("app_id") or "").strip()
    if not app_id:
        return ToolResult(success=False, error="`app_id` is required")
    client, headers = _client_and_headers(context)
    r = await client.delete(f"{_APPS_API}/{app_id}", headers=headers)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Deleted application id={app_id}.")


delete_app_tool = ToolDefinition(
    name="delete_app",
    description="Delete an application by id. Destructive — confirm before calling.",
    parameters=[
        ToolParameter(name="app_id", type="string", description="Application id to delete"),
    ],
    execute=_execute_delete_app,
)


async def _execute_whoami(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _client_and_headers(context)
    r = await client.get("/api/security/verifyToken", headers=headers)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    d = r.data if isinstance(r.data, dict) else {}
    user = d.get("user") or {}
    summary = (
        f"Authenticated as: {user.get('userName', '?')} (id={user.get('id', '?')})\n"
        f"clientCode: {d.get('loggedInClientCode', '?')}\n"
        f"verifiedAppCode: {d.get('verifiedAppCode', '?')}"
    )
    return ToolResult(success=True, summary=summary)


whoami_tool = ToolDefinition(
    name="whoami",
    description="Verify auth and report the authenticated user, clientCode, and verified appCode.",
    parameters=[],
    execute=_execute_whoami,
)


# ═════════════════════════════════════════════════════════════════════════
#  THEMES (5 tools)
# ═════════════════════════════════════════════════════════════════════════

_THEMES_API = "/api/ui/themes"


async def _execute_list_themes(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    r = await client.get(_THEMES_API, headers=headers, params={"page": 0, "size": _page_size(params, 100, 500), "appCode": ac})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    rows = [{
        "name": t.get("name"), "id": t.get("id"), "version": t.get("version"),
        "clientCode": t.get("clientCode"),
        "breakpoints": list((t.get("variables") or {}).keys()),
    } for t in content]
    return ToolResult(success=True, summary=f"Themes in '{ac}' ({len(rows)}):\n{json.dumps(rows, indent=2, default=str)}")


list_themes_tool = ToolDefinition(
    name="list_themes",
    description="List themes for an app with their breakpoints.",
    parameters=[
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="size", type="integer", required=False, default=100, description=_DESC_SIZE),
    ],
    execute=_execute_list_themes,
)


async def _execute_get_theme(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error=_ERR_NAME_REQUIRED)
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, _THEMES_API, ac, name)
    if err:
        return ToolResult(success=False, error=f"theme '{name}' {err}")
    body = json.dumps(doc, indent=2, default=str)
    total = len(body)
    offset = max(0, int(params.get("offset") or 0))
    max_chars = params.get("max_chars")
    header = f"Theme '{name}' (totalChars={total}, offset={offset}):\n\n"
    if offset:
        body = body[offset:]
    if max_chars:
        max_chars = int(max_chars)
        shown = body[:max_chars]
        suffix = ""
        if len(body) > max_chars:
            suffix = f"\n\n... [showing {max_chars} of {len(body)} chars; total {total}; call again with offset={offset + max_chars}]"
        return ToolResult(success=True, summary=header + shown + suffix)
    return ToolResult(success=True, summary=header + body)


get_theme_tool = ToolDefinition(
    name="get_theme",
    description="Read a theme's per-breakpoint variable maps. Supports offset/max_chars for chunked reads on large themes.",
    parameters=[
        ToolParameter(name="name", type="string", description="Theme name"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="offset", type="integer", required=False, default=0, description="Character offset for chunked reads"),
        ToolParameter(name="max_chars", type="integer", required=False, description="Cap on returned JSON length (default unlimited)"),
    ],
    execute=_execute_get_theme,
)


async def _execute_create_theme(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    variables = params.get("variables") or {}
    if not name or not isinstance(variables, dict):
        return ToolResult(success=False, error="`name` and `variables` (dict) are required")
    ne = c.validate_simple_name(name)
    if ne:
        return ToolResult(success=False, error=ne)
    for bp in variables:
        be = c.validate_breakpoint(bp)
        if be:
            return ToolResult(success=False, error=be)
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    cc = _resolve_client_code(params, context)
    body = {
        "name": name, "appCode": ac, "clientCode": cc,
        "variables": variables, "message": params.get("message") or _DEFAULT_CREATE_MESSAGE,
    }
    client, headers = _client_and_headers(context)
    r = await client.post(_THEMES_API, headers=headers, json=body)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Created theme '{name}' (id={(r.data or {}).get('id', '?')}).")


create_theme_tool = ToolDefinition(
    name="create_theme",
    description="Create a theme. `variables` keys must be valid breakpoints (ALL, DESKTOP_SCREEN, MOBILE_POTRAIT_SCREEN, etc.).",
    parameters=[
        ToolParameter(name="name", type="string", description="Theme name (letters/digits)"),
        ToolParameter(name="variables", type="object", description="Per-breakpoint variables: {ALL: {colorOne: '#50BC9B'}, MOBILE_POTRAIT_SCREEN_ONLY: {messageContainerWidth: '100vw'}, ...}"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description=_DESC_CLIENT_CODE),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default=_DEFAULT_CREATE_MESSAGE),
    ],
    execute=_execute_create_theme,
)


async def _execute_update_theme(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    variables = params.get("variables") or {}
    if not name or not isinstance(variables, dict):
        return ToolResult(success=False, error="`name` and `variables` are required")
    for bp in variables:
        be = c.validate_breakpoint(bp)
        if be:
            return ToolResult(success=False, error=be)
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, _THEMES_API, ac, name)
    if err or doc is None:
        return ToolResult(success=False, error=f"theme '{name}' {err or 'not found'}")
    doc["variables"] = variables
    doc["message"] = params.get("message") or _DEFAULT_UPDATE_MESSAGE
    save = await client.put(f"{_THEMES_API}/{doc.get('id')}", headers=headers, json=doc)
    if not save.success:
        return ToolResult(success=False, error=save.error)
    return ToolResult(success=True, summary=f"Updated theme '{name}'.")


update_theme_tool = ToolDefinition(
    name="update_theme",
    description="Replace a theme's variables (full-replacement, not merge). Fetch with get_theme(max_chars=large) first if you want to preserve other breakpoints.",
    parameters=[
        ToolParameter(name="name", type="string", description="Theme name to update"),
        ToolParameter(name="variables", type="object", description="Replacement per-breakpoint variable map"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default=_DEFAULT_UPDATE_MESSAGE),
    ],
    execute=_execute_update_theme,
)


async def _execute_delete_theme(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error=_ERR_NAME_REQUIRED)
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, _THEMES_API, ac, name)
    if err or doc is None:
        return ToolResult(success=False, error=f"theme '{name}' {err or 'not found'}")
    d = await client.delete(f"{_THEMES_API}/{doc.get('id')}", headers=headers)
    if not d.success:
        return ToolResult(success=False, error=d.error)
    return ToolResult(success=True, summary=f"Deleted theme '{name}' (id={doc.get('id')}).")


delete_theme_tool = ToolDefinition(
    name="delete_theme",
    description="Delete a theme. Pages that referenced it fall back to the app's default.",
    parameters=[
        ToolParameter(name="name", type="string", description="Theme name to delete"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ],
    execute=_execute_delete_theme,
)


# ═════════════════════════════════════════════════════════════════════════
#  STYLES (5 tools)
# ═════════════════════════════════════════════════════════════════════════

_STYLES_API = "/api/ui/styles"


async def _execute_list_styles(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    r = await client.get(_STYLES_API, headers=headers, params={"page": 0, "size": 100, "appCode": ac})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    rows = [{
        "name": s.get("name"), "id": s.get("id"), "version": s.get("version"),
        "clientCode": s.get("clientCode"),
        "cssLength": len(s.get("styleString") or ""),
    } for s in content]
    return ToolResult(success=True, summary=f"Styles in '{ac}' ({len(rows)}):\n{json.dumps(rows, indent=2, default=str)}")


list_styles_tool = ToolDefinition(
    name="list_styles",
    description="List style docs (raw global CSS dumps) for an app.",
    parameters=[
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ],
    execute=_execute_list_styles,
)


async def _execute_get_style(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error=_ERR_NAME_REQUIRED)
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, _STYLES_API, ac, name)
    if err or doc is None:
        return ToolResult(success=False, error=f"style '{name}' {err or 'not found'}")
    css_body = doc.get("styleString", "") or ""
    total = len(css_body)
    offset = max(0, int(params.get("offset") or 0))
    max_chars = params.get("max_chars")
    header = f"Style '{name}' (v{doc.get('version')}, clientCode={doc.get('clientCode')}, totalChars={total}, offset={offset}):\n\n"
    if offset:
        css_body = css_body[offset:]
    if max_chars:
        max_chars = int(max_chars)
        shown = css_body[:max_chars]
        suffix = ""
        if len(css_body) > max_chars:
            suffix = f"\n\n... [showing {max_chars} of {len(css_body)} chars; total {total}; call again with offset={offset + max_chars}]"
        return ToolResult(success=True, summary=header + shown + suffix)
    return ToolResult(success=True, summary=header + css_body)


get_style_tool = ToolDefinition(
    name="get_style",
    description="Read a style's raw CSS body. Supports offset/max_chars for chunked reads on large styles.",
    parameters=[
        ToolParameter(name="name", type="string", description="Style name"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="offset", type="integer", required=False, default=0, description="Character offset for chunked reads"),
        ToolParameter(name="max_chars", type="integer", required=False, description="Cap on returned body length"),
    ],
    execute=_execute_get_style,
)


async def _execute_create_style(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    css = params.get("css") or ""
    if not name:
        return ToolResult(success=False, error=_ERR_NAME_REQUIRED)
    ne = c.validate_simple_name(name)
    if ne:
        return ToolResult(success=False, error=ne)
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    cc = _resolve_client_code(params, context)
    body = {
        "name": name, "appCode": ac, "clientCode": cc, "styleString": css,
        "message": params.get("message") or _DEFAULT_CREATE_MESSAGE,
    }
    client, headers = _client_and_headers(context)
    r = await client.post(_STYLES_API, headers=headers, json=body)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Created style '{name}' (id={(r.data or {}).get('id', '?')}).")


create_style_tool = ToolDefinition(
    name="create_style",
    description="Create a global-CSS style doc. Use for app-wide rules / @keyframes / transitions that don't fit per-component styleProperties.",
    parameters=[
        ToolParameter(name="name", type="string", description="Style name (letters/digits)"),
        ToolParameter(name="css", type="string", description="Raw CSS string"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description=_DESC_CLIENT_CODE),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default=_DEFAULT_CREATE_MESSAGE),
    ],
    execute=_execute_create_style,
)


async def _execute_update_style(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    css = params.get("css")
    if not name or css is None:
        return ToolResult(success=False, error="`name` and `css` are required")
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, _STYLES_API, ac, name)
    if err or doc is None:
        return ToolResult(success=False, error=f"style '{name}' {err or 'not found'}")
    doc["styleString"] = css
    doc["message"] = params.get("message") or _DEFAULT_UPDATE_MESSAGE
    save = await client.put(f"{_STYLES_API}/{doc.get('id')}", headers=headers, json=doc)
    if not save.success:
        return ToolResult(success=False, error=save.error)
    return ToolResult(success=True, summary=f"Updated style '{name}'.")


update_style_tool = ToolDefinition(
    name="update_style",
    description="Replace a style's CSS body.",
    parameters=[
        ToolParameter(name="name", type="string", description="Style name to update"),
        ToolParameter(name="css", type="string", description="Replacement raw CSS string"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default=_DEFAULT_UPDATE_MESSAGE),
    ],
    execute=_execute_update_style,
)


async def _execute_delete_style(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error=_ERR_NAME_REQUIRED)
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, _STYLES_API, ac, name)
    if err or doc is None:
        return ToolResult(success=False, error=f"style '{name}' {err or 'not found'}")
    d = await client.delete(f"{_STYLES_API}/{doc.get('id')}", headers=headers)
    if not d.success:
        return ToolResult(success=False, error=d.error)
    return ToolResult(success=True, summary=f"Deleted style '{name}' (id={doc.get('id')}).")


delete_style_tool = ToolDefinition(
    name="delete_style",
    description="Delete a global-CSS style doc.",
    parameters=[
        ToolParameter(name="name", type="string", description="Style name to delete"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ],
    execute=_execute_delete_style,
)


# ═════════════════════════════════════════════════════════════════════════
#  URI PATHS (5 tools)
# ═════════════════════════════════════════════════════════════════════════

_URI_PATHS_API = "/api/ui/uripaths"
_VALID_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


async def _execute_list_uri_paths(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    r = await client.get(_URI_PATHS_API, headers=headers, params={"page": 0, "size": _page_size(params, 200, 1000), "appCode": ac})
    if not r.success:
        return ToolResult(success=False, error=r.error)
    content = (r.data or {}).get("content", []) if isinstance(r.data, dict) else []
    rows = [{
        "name": u.get("name"), "id": u.get("id"),
        "pathString": u.get("pathString"),
        "methods": list((u.get("pathDefinitions") or {}).keys()),
        "version": u.get("version"),
    } for u in content]
    return ToolResult(success=True, summary=f"URIPaths in '{ac}' ({len(rows)}):\n{json.dumps(rows, indent=2, default=str)}")


list_uri_paths_tool = ToolDefinition(
    name="list_uri_paths",
    description="List URIPaths in an app with their methods + target functions. URIPath = REST route → Kirun function call.",
    parameters=[
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="size", type="integer", required=False, default=200, description=_DESC_SIZE),
    ],
    execute=_execute_list_uri_paths,
)


async def _execute_get_uri_path(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error=_ERR_NAME_REQUIRED)
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, _URI_PATHS_API, ac, name)
    if err or doc is None:
        return ToolResult(success=False, error=f"URIPath '{name}' {err or 'not found'}")
    return ToolResult(success=True, summary=json.dumps(doc, indent=2, default=str))


get_uri_path_tool = ToolDefinition(
    name="get_uri_path",
    description="Read a URIPath's pathString + per-method Kirun function bindings.",
    parameters=[
        ToolParameter(name="name", type="string", description="URIPath name"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ],
    execute=_execute_get_uri_path,
)


def _validate_methods(path_defs: dict[str, Any]) -> str | None:
    for method in path_defs:
        if method.upper() not in _VALID_METHODS:
            return f"'{method}' is not a valid HTTP method. Valid: {_VALID_METHODS}"
    return None


async def _execute_create_uri_path(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    path_string = (params.get("path_string") or "").strip()
    path_definitions = params.get("path_definitions") or {}
    if not name or not path_string or not isinstance(path_definitions, dict):
        return ToolResult(success=False, error="`name`, `path_string`, `path_definitions` are required")
    me = _validate_methods(path_definitions)
    if me:
        return ToolResult(success=False, error=me)
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    cc = _resolve_client_code(params, context)
    body = {
        "name": name, "appCode": ac, "clientCode": cc,
        "pathString": path_string, "pathDefinitions": path_definitions,
        "message": params.get("message") or _DEFAULT_CREATE_MESSAGE,
    }
    client, headers = _client_and_headers(context)
    r = await client.post(_URI_PATHS_API, headers=headers, json=body)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Created URIPath '{name}' ({path_string}) bound to {list(path_definitions.keys())}.")


create_uri_path_tool = ToolDefinition(
    name="create_uri_path",
    description="Define a REST route that invokes a Kirun function. Path params declared as `{name}` in path_string become function arguments via pathParamMapping.",
    parameters=[
        ToolParameter(name="name", type="string", description="Logical name (may match pathString or be a slug)"),
        ToolParameter(name="path_string", type="string", description="URL template with named params, e.g. '/customers/{id}/invoices'"),
        ToolParameter(name="path_definitions", type="object", description="Per-method bindings: {GET: {uriType: 'KIRUN_FUNCTION', kiRunFxDefinition: {name, namespace, pathParamMapping: {pathParam: functionParam}}}, POST: {...}, ...}"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description=_DESC_CLIENT_CODE),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default=_DEFAULT_CREATE_MESSAGE),
    ],
    execute=_execute_create_uri_path,
)


async def _execute_update_uri_path(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error=_ERR_NAME_REQUIRED)
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    path_definitions = params.get("path_definitions")
    if path_definitions:
        me = _validate_methods(path_definitions)
        if me:
            return ToolResult(success=False, error=me)
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, _URI_PATHS_API, ac, name)
    if err or doc is None:
        return ToolResult(success=False, error=f"URIPath '{name}' {err or 'not found'}")
    changed: list[str] = []
    if params.get("path_string") is not None:
        doc["pathString"] = params["path_string"]
        changed.append("pathString")
    if path_definitions is not None:
        doc["pathDefinitions"] = path_definitions
        changed.append("pathDefinitions")
    if not changed:
        return ToolResult(success=True, summary="No-op: nothing to update.")
    doc["message"] = params.get("message") or _DEFAULT_UPDATE_MESSAGE
    save = await client.put(f"{_URI_PATHS_API}/{doc.get('id')}", headers=headers, json=doc)
    if not save.success:
        return ToolResult(success=False, error=save.error)
    return ToolResult(success=True, summary=f"Updated URIPath '{name}': {', '.join(changed)}.")


update_uri_path_tool = ToolDefinition(
    name="update_uri_path",
    description="Update a URIPath's path string and/or per-method function bindings.",
    parameters=[
        ToolParameter(name="name", type="string", description="URIPath name to update"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="path_string", type="string", required=False, description="New URL template"),
        ToolParameter(name="path_definitions", type="object", required=False, description="Replacement per-method bindings"),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default=_DEFAULT_UPDATE_MESSAGE),
    ],
    execute=_execute_update_uri_path,
)


async def _execute_delete_uri_path(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error=_ERR_NAME_REQUIRED)
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    doc, err = await _find_by_name(client, headers, _URI_PATHS_API, ac, name)
    if err or doc is None:
        return ToolResult(success=False, error=f"URIPath '{name}' {err or 'not found'}")
    d = await client.delete(f"{_URI_PATHS_API}/{doc.get('id')}", headers=headers)
    if not d.success:
        return ToolResult(success=False, error=d.error)
    return ToolResult(success=True, summary=f"Deleted URIPath '{name}' (id={doc.get('id')}).")


delete_uri_path_tool = ToolDefinition(
    name="delete_uri_path",
    description="Delete a URIPath. The REST endpoint stops responding immediately.",
    parameters=[
        ToolParameter(name="name", type="string", description="URIPath name to delete"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
    ],
    execute=_execute_delete_uri_path,
)


# ── Module export ────────────────────────────────────────────────────────


TOOLS: list[ToolDefinition] = [
    # apps (7)
    list_apps_tool, get_app_tool, create_app_tool, set_app_page_reference_tool,
    update_app_tool, delete_app_tool, whoami_tool,
    # themes (5)
    list_themes_tool, get_theme_tool, create_theme_tool, update_theme_tool, delete_theme_tool,
    # styles (5)
    list_styles_tool, get_style_tool, create_style_tool, update_style_tool, delete_style_tool,
    # uri_paths (5)
    list_uri_paths_tool, get_uri_path_tool, create_uri_path_tool, update_uri_path_tool, delete_uri_path_tool,
]
