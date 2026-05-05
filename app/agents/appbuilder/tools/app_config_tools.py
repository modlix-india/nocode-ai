"""Application config tools — deferred tools for app-level settings.

Covers: page references (defaultPage, loginPage, shellPage, etc.),
font/icon packs, metadata (title, favicon, CSP, PWA manifest, codeParts).
"""

from __future__ import annotations

from typing import Any

from app.core.tools.base import (
    ToolDefinition,
    ToolParameter,
    ToolResult,
    ResultTier,
)


def _get_client_and_headers(context: dict[str, Any]):
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context["headers"]


async def _read_and_update_app(
    app_id: str,
    updater: Any,  # callable(entity) -> None
    message: str,
    context: dict[str, Any],
) -> ToolResult:
    """Read an application definition, apply updates, and save."""
    from app.agents.appbuilder.tools._shared import save_entity
    client, headers = _get_client_and_headers(context)

    current = await client.get(f"/api/ui/applications/{app_id}", headers=headers)
    if not current.success:
        return ToolResult(success=False, error=f"Failed to read application: {current.error}")

    entity = current.data
    updater(entity)
    entity["message"] = message

    result = await save_entity(
        client, "/api/ui/applications", app_id, entity, headers, context.get("client_code", ""),
    )
    if not result.success:
        return result
    return ToolResult(success=True, summary=f"Updated application (id={app_id}).", result_tier=ResultTier.COMPACT)


# ── update_app_pages ─────────────────────────────────────────────


async def _execute_update_app_pages(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    app_id = params.get("id")
    if not app_id:
        return ToolResult(success=False, error="id (UI application definition ID) is required.")

    page_refs = params.get("page_refs", {})
    if not page_refs:
        return ToolResult(success=False, error="page_refs is required (e.g. {'defaultPage': 'home', 'loginPage': 'login'}).")

    def update(entity: dict) -> None:
        props = entity.setdefault("properties", {})
        for key, value in page_refs.items():
            if isinstance(value, str):
                props[key] = {"name": {"value": value}}
            elif isinstance(value, dict):
                props[key] = value

    keys = list(page_refs.keys())
    return await _read_and_update_app(
        app_id, update, params.get("message", f"Updated page refs: {keys}"), context,
    )


UPDATE_APP_PAGES = ToolDefinition(
    name="update_app_pages",
    description=(
        "Update application page references. Valid keys: defaultPage, loginPage, shellPage, "
        "forbiddenPage, notFoundPage, signUp, forgotPasswordPage, termsConditionPage, privacyPolicyPage."
    ),
    parameters=[
        ToolParameter(name="id", type="string", description="UI application definition ID.", required=True),
        ToolParameter(
            name="page_refs",
            type="object",
            description='Page references, e.g. {"defaultPage": "home", "shellPage": "shell"}.',
            required=True,
        ),
        ToolParameter(name="message", type="string", description="Change description.", required=True),
    ],
    execute=_execute_update_app_pages,
    is_deferred=True,
    search_hint="set default login shell page app configuration",
    result_tier=ResultTier.COMPACT,
)


# ── update_app_fonts ─────────────────────────────────────────────


async def _execute_update_app_fonts(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    app_id = params.get("id")
    if not app_id:
        return ToolResult(success=False, error="id is required.")

    font_packs = params.get("font_packs")
    icon_packs = params.get("icon_packs")
    if not font_packs and not icon_packs:
        return ToolResult(success=False, error="Provide font_packs and/or icon_packs.")

    def update(entity: dict) -> None:
        props = entity.setdefault("properties", {})
        if font_packs:
            props.setdefault("fontPacks", {}).update(font_packs)
        if icon_packs:
            props.setdefault("iconPacks", {}).update(icon_packs)

    return await _read_and_update_app(
        app_id, update, params.get("message", "Updated font/icon packs"), context,
    )


UPDATE_APP_FONTS = ToolDefinition(
    name="update_app_fonts",
    description="Add or update font packs (Google Fonts) and icon packs (FontAwesome, Material) on an application.",
    parameters=[
        ToolParameter(name="id", type="string", description="UI application definition ID.", required=True),
        ToolParameter(name="font_packs", type="object", description="Font packs to add/merge.", required=False),
        ToolParameter(name="icon_packs", type="object", description="Icon packs to add/merge.", required=False),
        ToolParameter(name="message", type="string", description="Change description.", required=True),
    ],
    execute=_execute_update_app_fonts,
    is_deferred=True,
    search_hint="add font icon pack Google Fonts material",
    result_tier=ResultTier.COMPACT,
)


# ── update_app_meta ──────────────────────────────────────────────


async def _execute_update_app_meta(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    app_id = params.get("id")
    if not app_id:
        return ToolResult(success=False, error="id is required.")

    updates = params.get("updates", {})
    if not updates:
        return ToolResult(success=False, error="updates is required.")

    def update(entity: dict) -> None:
        props = entity.setdefault("properties", {})
        for key, value in updates.items():
            if key in ("title", "csp", "cspReport", "manifest", "links", "codeParts"):
                if isinstance(value, dict):
                    props.setdefault(key, {}).update(value)
                else:
                    props[key] = value
            else:
                props[key] = value

    keys = list(updates.keys())
    return await _read_and_update_app(
        app_id, update, params.get("message", f"Updated app meta: {keys}"), context,
    )


UPDATE_APP_META = ToolDefinition(
    name="update_app_meta",
    description=(
        "Update application metadata: title, CSP, manifest (PWA), links (favicon), "
        "codeParts (custom HTML/JS injection at BEFORE_HEAD, AFTER_HEAD, BEFORE_BODY, AFTER_BODY)."
    ),
    parameters=[
        ToolParameter(name="id", type="string", description="UI application definition ID.", required=True),
        ToolParameter(
            name="updates",
            type="object",
            description="Key-value pairs to update. Keys: title, csp, cspReport, manifest, links, codeParts.",
            required=True,
        ),
        ToolParameter(name="message", type="string", description="Change description.", required=True),
    ],
    execute=_execute_update_app_meta,
    is_deferred=True,
    search_hint="update app title favicon CSP manifest PWA SEO",
    result_tier=ResultTier.COMPACT,
)


# ── Exports ──────────────────────────────────────────────────────

APP_CONFIG_TOOLS: list[ToolDefinition] = [
    UPDATE_APP_PAGES,
    UPDATE_APP_FONTS,
    UPDATE_APP_META,
]
