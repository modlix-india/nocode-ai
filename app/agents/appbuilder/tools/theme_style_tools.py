"""Theme & Style tools — deferred tools for design management.

For APPs: themes provide centralized design tokens, styles provide global CSS.
For SITEs: themes/styles are NOT used — colors go inline in components.
"""

from __future__ import annotations

import json
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


def _resolve_app_code(params: dict[str, Any], context: dict[str, Any]) -> tuple[str, ToolResult | None]:
    app_code = params.get("app_code") or context.get("app_code", "")
    if not app_code:
        return "", ToolResult(success=False, error="No appCode set.")
    return app_code, None


# ── create_theme ─────────────────────────────────────────────────


async def _execute_create_theme(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    if not params.get("confirmed"):
        return ToolResult(
            success=False,
            error="Theme creation affects the entire application. Describe the planned theme "
                  "to the user first, then call with confirmed=true after they agree.",
        )
    from app.agents.appbuilder.tools._shared import validate_name
    client, headers = _get_client_and_headers(context)
    app_code, err = _resolve_app_code(params, context)
    if err:
        return err
    name = params["name"]
    err = validate_name(name)
    if err:
        return err

    body = {
        "name": name,
        "appCode": app_code,
        "clientCode": context.get("client_code", ""),
        "variables": params.get("variables", {}),
        "message": params.get("message", f"Created theme '{name}'"),
    }
    result = await client.post("/api/ui/themes", headers=headers, json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to create theme: {result.error}")
    created = result.data
    return ToolResult(
        success=True,
        summary=f"Created theme '{name}' (id={created.get('id', '?')}).",
        result_tier=ResultTier.COMPACT,
    )


CREATE_THEME = ToolDefinition(
    name="create_theme",
    description="Create a new theme with design token variables per breakpoint. Requires confirmed=true after describing to user.",
    parameters=[
        ToolParameter(name="name", type="string", description="Theme name.", required=True),
        ToolParameter(name="variables", type="object", description='Variables by breakpoint, e.g. {"ALL": {"primaryColor": "#3B82F6"}}.', required=True),
        ToolParameter(name="confirmed", type="boolean", description="Must be true — confirm user approved theme.", required=True),
        ToolParameter(name="message", type="string", description="Change description.", required=True),
        ToolParameter(name="app_code", type="string", description="App code.", required=False),
    ],
    execute=_execute_create_theme,
    is_deferred=True,
    search_hint="create theme colors design tokens variables breakpoint",
    result_tier=ResultTier.COMPACT,
)


# ── update_theme ─────────────────────────────────────────────────


async def _execute_update_theme(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    if not params.get("confirmed"):
        return ToolResult(
            success=False,
            error="Theme updates affect the entire application. Describe changes to user first.",
        )
    from app.agents.appbuilder.tools._shared import save_entity
    client, headers = _get_client_and_headers(context)
    theme_id = params.get("id")
    if not theme_id:
        return ToolResult(success=False, error="id is required for theme update.")

    current = await client.get(f"/api/ui/themes/{theme_id}", headers=headers)
    if not current.success:
        return ToolResult(success=False, error=f"Failed to read theme: {current.error}")

    entity = current.data
    variables = params.get("variables", {})
    entity.setdefault("variables", {}).update(variables)
    entity["message"] = params.get("message", "Theme update")

    result = await save_entity(client, "/api/ui/themes", theme_id, entity, headers, context.get("client_code", ""))
    if not result.success:
        return result
    return ToolResult(success=True, summary=f"Updated theme (id={theme_id}).", result_tier=ResultTier.COMPACT)


UPDATE_THEME = ToolDefinition(
    name="update_theme",
    description="Update theme variables (merge by breakpoint). Requires confirmed=true after describing changes to user.",
    parameters=[
        ToolParameter(name="id", type="string", description="Theme ID.", required=True),
        ToolParameter(name="variables", type="object", description="Variables to merge by breakpoint.", required=True),
        ToolParameter(name="confirmed", type="boolean", description="Must be true.", required=True),
        ToolParameter(name="message", type="string", description="Change description.", required=True),
    ],
    execute=_execute_update_theme,
    is_deferred=True,
    search_hint="update theme color variable breakpoint responsive",
    result_tier=ResultTier.COMPACT,
)


# ── create_style ─────────────────────────────────────────────────


async def _execute_create_style(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    from app.agents.appbuilder.tools._shared import validate_name
    client, headers = _get_client_and_headers(context)
    app_code, err = _resolve_app_code(params, context)
    if err:
        return err
    name = params["name"]
    err = validate_name(name)
    if err:
        return err

    body = {
        "name": name,
        "appCode": app_code,
        "clientCode": context.get("client_code", ""),
        "definition": params.get("definition", {}),
        "message": params.get("message", f"Created style '{name}'"),
    }
    result = await client.post("/api/ui/styles", headers=headers, json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to create style: {result.error}")
    created = result.data
    return ToolResult(
        success=True,
        summary=f"Created style '{name}' (id={created.get('id', '?')}).",
        result_tier=ResultTier.COMPACT,
    )


CREATE_STYLE = ToolDefinition(
    name="create_style",
    description="Create a reusable global CSS style definition.",
    parameters=[
        ToolParameter(name="name", type="string", description="Style name.", required=True),
        ToolParameter(name="definition", type="object", description="Style definition object.", required=True),
        ToolParameter(name="message", type="string", description="Change description.", required=True),
        ToolParameter(name="app_code", type="string", description="App code.", required=False),
    ],
    execute=_execute_create_style,
    is_deferred=True,
    search_hint="create global CSS style definition reusable",
    result_tier=ResultTier.COMPACT,
)


# ── update_style ─────────────────────────────────────────────────


async def _execute_update_style(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    from app.agents.appbuilder.tools._shared import save_entity
    client, headers = _get_client_and_headers(context)
    style_id = params.get("id")
    if not style_id:
        return ToolResult(success=False, error="id is required for style update.")

    current = await client.get(f"/api/ui/styles/{style_id}", headers=headers)
    if not current.success:
        return ToolResult(success=False, error=f"Failed to read style: {current.error}")

    entity = current.data
    definition = params.get("definition", {})
    entity.setdefault("definition", {}).update(definition)
    entity["message"] = params.get("message", "Style update")

    result = await save_entity(client, "/api/ui/styles", style_id, entity, headers, context.get("client_code", ""))
    if not result.success:
        return result
    return ToolResult(success=True, summary=f"Updated style (id={style_id}).", result_tier=ResultTier.COMPACT)


UPDATE_STYLE = ToolDefinition(
    name="update_style",
    description="Update a global style definition (partial merge).",
    parameters=[
        ToolParameter(name="id", type="string", description="Style ID.", required=True),
        ToolParameter(name="definition", type="object", description="Definition fields to merge.", required=True),
        ToolParameter(name="message", type="string", description="Change description.", required=True),
    ],
    execute=_execute_update_style,
    is_deferred=True,
    search_hint="update global CSS style",
    result_tier=ResultTier.COMPACT,
)


# ── Exports ──────────────────────────────────────────────────────

THEME_STYLE_TOOLS: list[ToolDefinition] = [
    CREATE_THEME,
    UPDATE_THEME,
    CREATE_STYLE,
    UPDATE_STYLE,
]
