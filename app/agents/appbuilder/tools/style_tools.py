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
    from app.agents.appbuilder.tools._shared import require_app_code

    client, headers = _get_client_and_headers(context)
    app_code = params.get("app_code") or context.get("app_code", "")
    if not app_code:
        _, err = require_app_code(context)
        return err

    result = await client.get(
        "/api/ui/themes",
        headers=headers,
        params={"page": 0, "size": 1000, "appCode": app_code},
    )
    if not result.success:
        return ToolResult(success=False, error=f"Failed to list themes: {result.error}")

    data = result.data
    themes = data.get("content", []) if isinstance(data, dict) else []
    lines = [f"- {t.get('name', '?')} (id={t.get('id', '?')}, v{t.get('version', '?')})" for t in themes]

    return ToolResult(
        success=True,
        data=[{"name": t.get("name"), "id": t.get("id"), "version": t.get("version")} for t in themes],
        summary=f"Found {len(themes)} themes:\n" + "\n".join(lines),
    )


async def _create_theme_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    from app.agents.appbuilder.tools._shared import validate_name, require_app_code

    if not params.get("confirmed"):
        return ToolResult(
            success=False,
            error=(
                "Theme creation affects the entire application — all components that reference "
                "Theme.* variables will be impacted. "
                "You MUST describe the planned theme (its variable names and values) to the user "
                "in plain text first, then only call this tool with confirmed=true after they explicitly agree."
            ),
        )

    client, headers = _get_client_and_headers(context)
    app_code = params.get("app_code") or context.get("app_code", "")
    if not app_code:
        _, err = require_app_code(context)
        return err
    name = params["name"]

    err = validate_name(name)
    if err:
        return err

    body: dict[str, Any] = {
        "name": name,
        "appCode": app_code,
        "clientCode": context.get("client_code", ""),
        "variables": params.get("variables", {}),
    }
    if params.get("title"):
        body["title"] = params["title"]
    if params.get("description"):
        body["description"] = params["description"]
    body["message"] = params["message"]

    result = await client.post("/api/ui/themes", headers=headers, json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to create theme: {result.error}")

    created = result.data
    theme_id = created.get("id", "?") if isinstance(created, dict) else "?"
    return ToolResult(success=True, summary=f"Created theme '{name}' (id={theme_id}).")


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
    if not params.get("confirmed"):
        return ToolResult(
            success=False,
            error=(
                "Theme updates affect the entire application — all components that reference "
                "Theme.* variables will be impacted. "
                "You MUST describe the planned changes (which variable names and their new values) "
                "to the user in plain text first, then only call this tool with confirmed=true "
                "after they explicitly agree."
            ),
        )

    client, headers = _get_client_and_headers(context)
    theme_id = params["theme_id"]
    variables = params.get("variables", {})

    # Fetch current theme
    current = await client.get(f"/api/ui/themes/{theme_id}", headers=headers)
    if not current.success:
        return ToolResult(success=False, error=f"Failed to read theme: {current.error}")

    theme_data = current.data
    theme_data.setdefault("variables", {}).update(variables)
    if params.get("title"):
        theme_data["title"] = params["title"]
    if params.get("description"):
        theme_data["description"] = params["description"]
    theme_data["message"] = params["message"]

    from app.agents.appbuilder.tools._shared import save_entity
    result = await save_entity(client, "/api/ui/themes", theme_id, theme_data, headers, context.get("client_code", ""))
    if not result.success:
        return result

    return ToolResult(success=True, summary=f"Updated theme (id={theme_id}).")


list_themes = ToolDefinition(
    name="list_themes", display_name="List Themes", description="List all themes in an application.",
    parameters=[ToolParameter(name="app_code", type="string", description="Application code.", required=False)],
    execute=_list_themes_execute,
)

create_theme = ToolDefinition(
    name="create_theme",
    display_name="Create Theme",
    description=(
        "Create a new theme — a named set of design tokens (camelCase key-value pairs) "
        "organized by screen-resolution breakpoints (ALL, MOBILE_POTRAIT_SCREEN_ONLY, etc.). "
        "Variables are referenced in component style/property expressions as Theme.variableName. "
        "IMPORTANT: Themes affect the ENTIRE application. You MUST describe the planned theme "
        "(variable names and values) to the user in plain text and get their explicit confirmation "
        "before calling this tool. Set confirmed=true only after the user agrees."
    ),
    parameters=[
        ToolParameter(name="name", type="string", description="Theme name (letters only)."),
        ToolParameter(
            name="variables", type="object", required=False,
            description=(
                "Theme variables by breakpoint. Keys are breakpoints "
                "(ALL, WIDE_SCREEN, DESKTOP_SCREEN, TABLET_LANDSCAPE_SCREEN, "
                "TABLET_LANDSCAPE_SCREEN_ONLY, TABLET_POTRAIT_SCREEN, TABLET_POTRAIT_SCREEN_ONLY, "
                "MOBILE_LANDSCAPE_SCREEN, MOBILE_LANDSCAPE_SCREEN_ONLY, "
                "MOBILE_POTRAIT_SCREEN, MOBILE_POTRAIT_SCREEN_ONLY). "
                "Each value is an object of camelCase name→value pairs, e.g. "
                "{\"ALL\": {\"primaryColor\": \"#3B82F6\", \"fontSizeBase\": \"16px\"}}."
            ),
        ),
        ToolParameter(name="title", type="string", description="Theme title.", required=False),
        ToolParameter(name="description", type="string", description="Theme description.", required=False),
        ToolParameter(name="message", type="string", description="Commit message (10–15 words) describing what was changed."),
        ToolParameter(name="confirmed", type="boolean", description="Must be true — confirms the user has been informed and approved this app-wide change."),
        ToolParameter(name="app_code", type="string", description="Application code.", required=False),
    ],
    execute=_create_theme_execute,
)

read_theme = ToolDefinition(
    name="read_theme", display_name="Read Theme", description="Read a theme's full definition.",
    parameters=[ToolParameter(name="theme_id", type="string", description="Theme ID.")],
    execute=_read_theme_execute,
)

update_theme = ToolDefinition(
    name="update_theme",
    display_name="Update Theme",
    description=(
        "Update a theme's variables (partial merge per breakpoint). "
        "Variables are camelCase design tokens referenced as Theme.variableName in expressions. "
        "IMPORTANT: Theme changes affect the ENTIRE application — every component referencing "
        "Theme.* variables will be impacted. You MUST describe the planned changes "
        "(which variable names and their new values) to the user in plain text and get their "
        "explicit confirmation before calling this tool. Set confirmed=true only after the user agrees."
    ),
    parameters=[
        ToolParameter(name="theme_id", type="string", description="Theme ID."),
        ToolParameter(
            name="variables", type="object",
            description=(
                "Variables to merge by breakpoint. Same structure as create_theme. "
                "E.g. {\"ALL\": {\"primaryColor\": \"#FF0000\"}} changes primaryColor for all screens."
            ),
        ),
        ToolParameter(name="title", type="string", description="Theme title.", required=False),
        ToolParameter(name="description", type="string", description="Theme description.", required=False),
        ToolParameter(name="message", type="string", description="Commit message (10–15 words) describing what was changed."),
        ToolParameter(name="confirmed", type="boolean", description="Must be true — confirms the user has been informed and approved this app-wide change."),
    ],
    execute=_update_theme_execute,
)


# ── Style tools ─────────────────────────────────────────────────

async def _list_styles_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    from app.agents.appbuilder.tools._shared import require_app_code

    client, headers = _get_client_and_headers(context)
    app_code = params.get("app_code") or context.get("app_code", "")
    if not app_code:
        _, err = require_app_code(context)
        return err

    result = await client.get(
        "/api/ui/styles",
        headers=headers,
        params={"page": 0, "size": 1000, "appCode": app_code},
    )
    if not result.success:
        return ToolResult(success=False, error=f"Failed to list styles: {result.error}")

    data = result.data
    styles = data.get("content", []) if isinstance(data, dict) else []
    lines = [f"- {s.get('name', '?')} (id={s.get('id', '?')}, v{s.get('version', '?')})" for s in styles]

    return ToolResult(
        success=True,
        data=[{"name": s.get("name"), "id": s.get("id"), "version": s.get("version")} for s in styles],
        summary=f"Found {len(styles)} styles:\n" + "\n".join(lines),
    )


async def _create_style_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    from app.agents.appbuilder.tools._shared import validate_name, require_app_code

    client, headers = _get_client_and_headers(context)
    app_code = params.get("app_code") or context.get("app_code", "")
    if not app_code:
        _, err = require_app_code(context)
        return err
    name = params["name"]

    err = validate_name(name)
    if err:
        return err

    body: dict[str, Any] = {
        "name": name,
        "appCode": app_code,
        "clientCode": context.get("client_code", ""),
        "definition": params.get("definition", {}),
    }
    if params.get("title"):
        body["title"] = params["title"]
    if params.get("description"):
        body["description"] = params["description"]
    body["message"] = params["message"]

    result = await client.post("/api/ui/styles", headers=headers, json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to create style: {result.error}")

    created = result.data
    style_id = created.get("id", "?") if isinstance(created, dict) else "?"
    return ToolResult(success=True, summary=f"Created style '{name}' (id={style_id}).")


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
    if params.get("title"):
        style_data["title"] = params["title"]
    if params.get("description"):
        style_data["description"] = params["description"]
    style_data["message"] = params["message"]

    from app.agents.appbuilder.tools._shared import save_entity
    result = await save_entity(client, "/api/ui/styles", style_id, style_data, headers, context.get("client_code", ""))
    if not result.success:
        return result

    return ToolResult(success=True, summary=f"Updated style (id={style_id}).")


list_styles = ToolDefinition(
    name="list_styles", display_name="List Styles", description="List all reusable style definitions in an application.",
    parameters=[ToolParameter(name="app_code", type="string", description="Application code.", required=False)],
    execute=_list_styles_execute,
)

create_style = ToolDefinition(
    name="create_style", display_name="Create Style", description="Create a new reusable style definition.",
    parameters=[
        ToolParameter(name="name", type="string", description="Style name (letters only)."),
        ToolParameter(name="definition", type="object", description="Style definition with responsive breakpoints.", required=False),
        ToolParameter(name="title", type="string", description="Style title.", required=False),
        ToolParameter(name="description", type="string", description="Style description.", required=False),
        ToolParameter(name="message", type="string", description="Commit message (10–15 words) describing what was changed."),
        ToolParameter(name="app_code", type="string", description="Application code.", required=False),
    ],
    execute=_create_style_execute,
)

read_style = ToolDefinition(
    name="read_style", display_name="Read Style", description="Read a style definition.",
    parameters=[ToolParameter(name="style_id", type="string", description="Style ID.")],
    execute=_read_style_execute,
)

update_style = ToolDefinition(
    name="update_style", display_name="Update Style", description="Update a style definition (partial merge).",
    parameters=[
        ToolParameter(name="style_id", type="string", description="Style ID."),
        ToolParameter(name="definition", type="object", description="Definition to merge."),
        ToolParameter(name="title", type="string", description="Style title.", required=False),
        ToolParameter(name="description", type="string", description="Style description.", required=False),
        ToolParameter(name="message", type="string", description="Commit message (10–15 words) describing what was changed."),
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
