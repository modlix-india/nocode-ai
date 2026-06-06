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
    p: dict[str, Any] = {"page": max(0, int(params.get("page") or 0)), "size": _page_size(params, 50, 500)}
    if params.get("name_filter"):
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
    description="List applications visible to the caller (via ClientHierarchy). Returns appCode + name + type + accessType.",
    parameters=[
        ToolParameter(name="page", type="integer", required=False, default=0, description="Zero-indexed page"),
        ToolParameter(name="size", type="integer", required=False, default=50, description=_DESC_SIZE),
        ToolParameter(name="name_filter", type="string", required=False, description="Server-side substring filter on appCode/name"),
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


async def _execute_create_app(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    app_code = (params.get("app_code") or "").strip()
    name = (params.get("name") or "").strip()
    if not app_code or not name:
        return ToolResult(success=False, error="`app_code` and `name` are required")
    ne = c.validate_simple_name(app_code)
    if ne:
        return ToolResult(success=False, error=ne)
    cc = _resolve_client_code(params, context)
    body: dict[str, Any] = {
        "appCode": app_code, "name": name, "clientCode": cc,
        "properties": params.get("properties") or {},
        "message": params.get("message") or "Created via CFA",
    }
    if params.get("languages") is not None:
        body["languages"] = params["languages"]
    if params.get("translations") is not None:
        body["translations"] = params["translations"]
    client, headers = _client_and_headers(context)
    r = await client.post(_APPS_API, headers=headers, json=body)
    if not r.success:
        return ToolResult(success=False, error=r.error)
    return ToolResult(success=True, summary=f"Created application '{app_code}' (id={(r.data or {}).get('id', '?')}).")


create_app_tool = ToolDefinition(
    name="create_app",
    description="Create a new application. After creation, future tools default to this appCode if set in context.",
    parameters=[
        ToolParameter(name="app_code", type="string", description="Unique appCode (letters/digits)"),
        ToolParameter(name="name", type="string", description="Display name"),
        ToolParameter(name="client_code", type="string", required=False, description="Owning clientCode"),
        ToolParameter(name="properties", type="object", required=False, description="App properties (defaultPage, loginPage, shellPage, themes, etc.) — top-level fields, not wrapped in {value:...}"),
        ToolParameter(name="languages", type="array", required=False, description="Supported locale codes, e.g. ['en','hi','ar']"),
        ToolParameter(name="translations", type="object", required=False, description="Translation map: {locale: {translationKey: translatedString}}"),
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default="Created via CFA"),
    ],
    execute=_execute_create_app,
)


_VALID_SLOTS = ("defaultPage", "loginPage", "shellPage", "forbiddenPage")


async def _execute_set_app_page_reference(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    slot = (params.get("slot") or "").strip()
    page_name = (params.get("page_name") or "").strip()
    if slot not in _VALID_SLOTS:
        return ToolResult(success=False, error=f"`slot` must be one of {_VALID_SLOTS}")
    if not page_name:
        return ToolResult(success=False, error="`page_name` is required")
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    client, headers = _client_and_headers(context)
    listing = await client.get(_APPS_API, headers=headers, params={"page": 0, "size": 50})
    if not listing.success:
        return ToolResult(success=False, error=listing.error)
    rows = (listing.data or {}).get("content", []) if isinstance(listing.data, dict) else []
    match = next((a for a in rows if a.get("appCode") == ac), None)
    if not match:
        return ToolResult(success=False, error=f"application '{ac}' not found.")
    detail = await client.get(f"{_APPS_API}/{match.get('id')}", headers=headers)
    if not detail.success:
        return ToolResult(success=False, error=detail.error)
    doc = detail.data if isinstance(detail.data, dict) else {}
    props = dict(doc.get("properties") or {})
    props[slot] = page_name
    doc["properties"] = props
    doc["message"] = params.get("message") or "Updated app page reference via CFA"
    save = await client.put(f"{_APPS_API}/{match.get('id')}", headers=headers, json=doc)
    if not save.success:
        return ToolResult(success=False, error=save.error)
    return ToolResult(success=True, summary=f"Set {slot}='{page_name}' on '{ac}'.")


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
        body.setdefault("properties", {}).update(params["properties"])
    if params.get("languages") is not None:
        body["languages"] = params["languages"]
    if params.get("default_language") is not None:
        body["defaultLanguage"] = params["default_language"]
    if params.get("version") is not None:
        body["version"] = params["version"]
    body["message"] = params.get("message") or "Updated via CFA"
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
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default="Updated via CFA"),
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
        return ToolResult(success=False, error="`name` is required")
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
        "variables": variables, "message": params.get("message") or "Created via CFA",
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
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default="Created via CFA"),
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
    doc["message"] = params.get("message") or "Updated via CFA"
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
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default="Updated via CFA"),
    ],
    execute=_execute_update_theme,
)


async def _execute_delete_theme(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
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
        return ToolResult(success=False, error="`name` is required")
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
        return ToolResult(success=False, error="`name` is required")
    ne = c.validate_simple_name(name)
    if ne:
        return ToolResult(success=False, error=ne)
    ac = _resolve_app_code(params, context)
    if not ac:
        return _err_app_code()
    cc = _resolve_client_code(params, context)
    body = {
        "name": name, "appCode": ac, "clientCode": cc, "styleString": css,
        "message": params.get("message") or "Created via CFA",
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
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default="Created via CFA"),
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
    doc["message"] = params.get("message") or "Updated via CFA"
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
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default="Updated via CFA"),
    ],
    execute=_execute_update_style,
)


async def _execute_delete_style(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
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
        return ToolResult(success=False, error="`name` is required")
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
        "message": params.get("message") or "Created via CFA",
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
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default="Created via CFA"),
    ],
    execute=_execute_create_uri_path,
)


async def _execute_update_uri_path(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
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
    doc["message"] = params.get("message") or "Updated via CFA"
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
        ToolParameter(name="message", type="string", required=False, description=_DESC_COMMIT_MSG, default="Updated via CFA"),
    ],
    execute=_execute_update_uri_path,
)


async def _execute_delete_uri_path(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    name = (params.get("name") or "").strip()
    if not name:
        return ToolResult(success=False, error="`name` is required")
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
