"""Style and theme management tools.

Themes: /api/ui/themes — define color schemes, fonts, variables.
Styles: /api/ui/styles — reusable named style definitions.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.tools.base import ToolDefinition, ToolResult, ToolParameter
from app.core.tools.http_client import SaasClient


def _get_client_and_headers(context: dict[str, Any]) -> tuple[SaasClient, dict[str, str]]:
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context["headers"]


# ── Theme tools ─────────────────────────────────────────────────

async def _list_themes_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    app_code = params.get("app_code", context.get("app_code", ""))

    result = await client.post(
        "/api/ui/themes/query",
        headers=headers,
        json={"page": 0, "size": 50, "condition": {"k": "appCode", "v": app_code}},
    )
    if not result.success:
        return ToolResult(success=False, error=f"Failed to list themes: {result.error}")

    data = result.data
    themes = data.get("content", []) if isinstance(data, dict) else []
    lines = [f"- {t.get('name', '?')} (id={t.get('id', '?')})" for t in themes]

    return ToolResult(
        success=True,
        data=[{"name": t.get("name"), "id": t.get("id")} for t in themes],
        summary=f"Found {len(themes)} themes:\n" + "\n".join(lines),
    )


async def _create_theme_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    app_code = params.get("app_code", context.get("app_code", ""))

    body = {
        "name": params["name"],
        "appCode": app_code,
        "clientCode": context.get("client_code", ""),
        "variables": params.get("variables", {}),
    }

    result = await client.post("/api/ui/themes", headers=headers, json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to create theme: {result.error}")

    created = result.data
    theme_id = created.get("id", "?") if isinstance(created, dict) else "?"
    return ToolResult(success=True, summary=f"Created theme '{params['name']}' (id={theme_id}).")


async def _read_theme_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    theme_id = params["theme_id"]

    result = await client.get(f"/api/ui/themes/{theme_id}", headers=headers)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to read theme: {result.error}")

    return ToolResult(
        success=True,
        data=result.data,
        summary=f"Theme:\n{json.dumps(result.data, indent=2, default=str)[:2000]}",
    )


async def _update_theme_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    theme_id = params["theme_id"]
    variables = params.get("variables", {})

    # Fetch current theme
    current = await client.get(f"/api/ui/themes/{theme_id}", headers=headers)
    if not current.success:
        return ToolResult(success=False, error=f"Failed to read theme: {current.error}")

    theme_data = current.data
    theme_data.setdefault("variables", {}).update(variables)

    result = await client.put(f"/api/ui/themes/{theme_id}", headers=headers, json=theme_data)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to update theme: {result.error}")

    return ToolResult(success=True, summary=f"Updated theme (id={theme_id}) variables: {list(variables.keys())}.")


list_themes = ToolDefinition(
    name="list_themes", description="List all themes in an application.",
    parameters=[ToolParameter(name="app_code", type="string", description="Application code.", required=False)],
    execute=_list_themes_execute,
)

create_theme = ToolDefinition(
    name="create_theme", description="Create a new theme with CSS variables for colors, fonts, etc.",
    parameters=[
        ToolParameter(name="name", type="string", description="Theme name."),
        ToolParameter(name="variables", type="object", description="CSS variable definitions.", required=False),
        ToolParameter(name="app_code", type="string", description="Application code.", required=False),
    ],
    execute=_create_theme_execute,
)

read_theme = ToolDefinition(
    name="read_theme", description="Read a theme's full definition.",
    parameters=[ToolParameter(name="theme_id", type="string", description="Theme ID.")],
    execute=_read_theme_execute,
)

update_theme = ToolDefinition(
    name="update_theme", description="Update a theme's variables (partial merge).",
    parameters=[
        ToolParameter(name="theme_id", type="string", description="Theme ID."),
        ToolParameter(name="variables", type="object", description="Variables to merge."),
    ],
    execute=_update_theme_execute,
)


# ── Style tools ─────────────────────────────────────────────────

async def _list_styles_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    app_code = params.get("app_code", context.get("app_code", ""))

    result = await client.post(
        "/api/ui/styles/query",
        headers=headers,
        json={"page": 0, "size": 50, "condition": {"k": "appCode", "v": app_code}},
    )
    if not result.success:
        return ToolResult(success=False, error=f"Failed to list styles: {result.error}")

    data = result.data
    styles = data.get("content", []) if isinstance(data, dict) else []
    lines = [f"- {s.get('name', '?')} (id={s.get('id', '?')})" for s in styles]

    return ToolResult(
        success=True,
        data=[{"name": s.get("name"), "id": s.get("id")} for s in styles],
        summary=f"Found {len(styles)} styles:\n" + "\n".join(lines),
    )


async def _create_style_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    app_code = params.get("app_code", context.get("app_code", ""))

    body = {
        "name": params["name"],
        "appCode": app_code,
        "clientCode": context.get("client_code", ""),
        "definition": params.get("definition", {}),
    }

    result = await client.post("/api/ui/styles", headers=headers, json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to create style: {result.error}")

    created = result.data
    style_id = created.get("id", "?") if isinstance(created, dict) else "?"
    return ToolResult(success=True, summary=f"Created style '{params['name']}' (id={style_id}).")


async def _read_style_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    style_id = params["style_id"]

    result = await client.get(f"/api/ui/styles/{style_id}", headers=headers)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to read style: {result.error}")

    return ToolResult(
        success=True,
        data=result.data,
        summary=f"Style:\n{json.dumps(result.data, indent=2, default=str)[:2000]}",
    )


async def _update_style_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    style_id = params["style_id"]
    definition = params.get("definition", {})

    current = await client.get(f"/api/ui/styles/{style_id}", headers=headers)
    if not current.success:
        return ToolResult(success=False, error=f"Failed to read style: {current.error}")

    style_data = current.data
    style_data.setdefault("definition", {}).update(definition)

    result = await client.put(f"/api/ui/styles/{style_id}", headers=headers, json=style_data)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to update style: {result.error}")

    return ToolResult(success=True, summary=f"Updated style (id={style_id}).")


list_styles = ToolDefinition(
    name="list_styles", description="List all reusable style definitions in an application.",
    parameters=[ToolParameter(name="app_code", type="string", description="Application code.", required=False)],
    execute=_list_styles_execute,
)

create_style = ToolDefinition(
    name="create_style", description="Create a new reusable style definition.",
    parameters=[
        ToolParameter(name="name", type="string", description="Style name."),
        ToolParameter(name="definition", type="object", description="Style definition with responsive breakpoints.", required=False),
        ToolParameter(name="app_code", type="string", description="Application code.", required=False),
    ],
    execute=_create_style_execute,
)

read_style = ToolDefinition(
    name="read_style", description="Read a style definition.",
    parameters=[ToolParameter(name="style_id", type="string", description="Style ID.")],
    execute=_read_style_execute,
)

update_style = ToolDefinition(
    name="update_style", description="Update a style definition (partial merge).",
    parameters=[
        ToolParameter(name="style_id", type="string", description="Style ID."),
        ToolParameter(name="definition", type="object", description="Definition to merge."),
    ],
    execute=_update_style_execute,
)


# ── Export ───────────────────────────────────────────────────────

STYLE_TOOLS: list[ToolDefinition] = [
    list_themes,
    create_theme,
    read_theme,
    update_theme,
    list_styles,
    create_style,
    read_style,
    update_style,
]
