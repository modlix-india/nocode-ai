"""Application management tools — CRUD via Multi and UI services.

Applications are managed through the Multi service (/api/multi/application),
listed/read through UI service (/api/ui/applications).
"""

from __future__ import annotations

import json
from typing import Any

from app.core.tools.base import ToolDefinition, ToolResult, ToolParameter
from app.core.tools.http_client import SaasClient


def _get_client_and_headers(context: dict[str, Any]) -> tuple[SaasClient, dict[str, str]]:
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context["headers"]


# ── create_application ──────────────────────────────────────────

async def _create_application_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    from app.agents.appbuilder.tools._shared import validate_name

    client, headers = _get_client_and_headers(context)
    app_name = params["app_name"]
    app_code = params["app_code"]
    app_type = params.get("app_type", "APP")

    err = validate_name(app_code)
    if err:
        return err

    body: dict[str, Any] = {
        "appName": app_name,
        "appCode": app_code,
        "appType": app_type,
    }
    body["message"] = params["message"]

    result = await client.post("/api/multi/application", headers={**headers}, json=body)

    if not result.success:
        return ToolResult(success=False, error=f"Failed to create application: {result.error}")

    # Track created app in session context
    session_ctx = context.get("session_context")
    if session_ctx is not None:
        session_ctx["app_code"] = app_code
        app_codes = session_ctx.setdefault("app_codes", [])
        if app_code not in app_codes:
            app_codes.append(app_code)

    return ToolResult(
        success=True,
        data=result.data,
        summary=f"Created application '{app_name}' (code={app_code}, type={app_type}).",
    )


create_application = ToolDefinition(
    name="create_application",
    description="Create a new application. Returns the created application details.",
    parameters=[
        ToolParameter(name="app_name", type="string", description="Display name for the application."),
        ToolParameter(name="app_code", type="string", description="Unique code (letters only, e.g. 'taskmanager')."),
        ToolParameter(name="app_type", type="string", description="Application type: 'APP' or 'SITE'.", required=False),
        ToolParameter(name="message", type="string", description="Commit message (10–15 words) describing what was changed."),
    ],
    execute=_create_application_execute,
)


# ── list_applications ───────────────────────────────────────────

async def _list_applications_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)

    body: dict[str, Any] = {"page": 0, "size": 100}

    # Search both appName and appCode with OR — the user may provide either
    name_filter = params.get("app_code", "")
    if name_filter:
        body["condition"] = {
            "conditions": [
                {"field": "appName", "value": name_filter, "operator": "STRING_LOOSE_EQUAL"},
                {"field": "appCode", "value": name_filter, "operator": "STRING_LOOSE_EQUAL"},
            ],
            "operator": "OR",
        }

    result = await client.post("/api/security/applications/query", headers=headers, json=body)

    if not result.success:
        return ToolResult(success=False, error=f"Failed to list applications: {result.error}")

    data = result.data
    apps = data.get("content", []) if isinstance(data, dict) else []
    lines = []
    for app in apps:
        name = app.get("appCode", app.get("name", "?"))
        app_id = app.get("id", "?")
        version = app.get("version", "?")
        lines.append(f"- {name} (id={app_id}, version={version})")

    # Track found app codes in session context
    session_ctx = context.get("session_context")
    if session_ctx is not None and apps:
        found_codes = [a.get("appCode") for a in apps if a.get("appCode")]
        app_codes = session_ctx.setdefault("app_codes", [])
        for code in found_codes:
            if code not in app_codes:
                app_codes.append(code)
        # If exactly one app found, set it as the working app
        if len(found_codes) == 1:
            session_ctx["app_code"] = found_codes[0]

    summary = f"Found {len(apps)} application(s):\n" + "\n".join(lines)
    return ToolResult(
        success=True,
        data=[{"name": a.get("appCode", a.get("name")), "id": a.get("id"), "version": a.get("version")} for a in apps],
        summary=summary,
    )


list_applications = ToolDefinition(
    name="list_applications",
    description="Search for applications by name or code. Searches both appName and appCode fields. IMPORTANT: Always call this first to confirm the exact appCode before using any other tool (pages, styles, functions, etc.).",
    parameters=[
        ToolParameter(name="app_code", type="string", description="Search term — matches against both appName and appCode.", required=False),
    ],
    execute=_list_applications_execute,
)


# ── read_application ────────────────────────────────────────────

async def _read_application_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    application_id = params["application_id"]

    result = await client.get(f"/api/ui/applications/{application_id}", headers=headers)

    if not result.success:
        return ToolResult(success=False, error=f"Failed to read application: {result.error}")

    app_data = result.data
    app_code = app_data.get("appCode", "?") if isinstance(app_data, dict) else "?"
    return ToolResult(
        success=True,
        data=app_data,
        summary=f"Application '{app_code}':\n{json.dumps(app_data, indent=2, default=str)[:2000]}",
    )


read_application = ToolDefinition(
    name="read_application",
    description="Read an application's full definition by its ID. Returns properties including named pages (defaultPage, loginPage, shellPage, forbiddenPage, notFoundPage, etc.), themes, styles, and other app-level settings. Use this after list_applications to understand the app structure before modifying pages.",
    parameters=[
        ToolParameter(name="application_id", type="string", description="Application ID (from list_applications)."),
    ],
    execute=_read_application_execute,
)


# ── update_application ─────────────────────────────────────────

async def _update_application_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    application_id = params["application_id"]

    current = await client.get(f"/api/ui/applications/{application_id}", headers=headers)
    if not current.success:
        return ToolResult(success=False, error=f"Failed to read application: {current.error}")

    app_data = current.data
    if params.get("properties"):
        app_data.setdefault("properties", {}).update(params["properties"])
    if params.get("title"):
        app_data["title"] = params["title"]
    if params.get("description"):
        app_data["description"] = params["description"]
    app_data["message"] = params["message"]

    from app.agents.appbuilder.tools._shared import save_entity
    result = await save_entity(client, "/api/ui/applications", application_id, app_data, headers, context.get("client_code", ""))
    if not result.success:
        return result

    return ToolResult(success=True, summary=f"Updated application (id={application_id}).")


update_application = ToolDefinition(
    name="update_application",
    description="Update an application's properties (title, description, themes, styles, etc.).",
    parameters=[
        ToolParameter(name="application_id", type="string", description="Application ID."),
        ToolParameter(name="properties", type="object", description="Properties to merge into the application.", required=False),
        ToolParameter(name="title", type="string", description="Application title.", required=False),
        ToolParameter(name="description", type="string", description="Application description.", required=False),
        ToolParameter(name="message", type="string", description="Commit message (10–15 words) describing what was changed."),
    ],
    execute=_update_application_execute,
)


# ── delete_application ──────────────────────────────────────────

async def _delete_application_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    app_code = params["app_code"]

    result = await client.delete(f"/api/multi/application/{app_code}", headers=headers)

    if not result.success:
        return ToolResult(success=False, error=f"Failed to delete application: {result.error}")

    return ToolResult(success=True, summary=f"Deleted application '{app_code}'.")


delete_application = ToolDefinition(
    name="delete_application",
    description="Delete an application by its app code. This is destructive and cannot be undone.",
    parameters=[
        ToolParameter(name="app_code", type="string", description="Application code to delete."),
    ],
    execute=_delete_application_execute,
)


# ── Export ───────────────────────────────────────────────────────

APPLICATION_TOOLS: list[ToolDefinition] = [
    create_application,
    list_applications,
    read_application,
    update_application,
    delete_application,
]
