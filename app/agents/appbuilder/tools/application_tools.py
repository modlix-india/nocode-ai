"""Application management tools — CRUD via Multi service.

Applications are managed through the Multi service (/api/multi/application)
and listed through Security service (/api/security/applications).
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
    client, headers = _get_client_and_headers(context)
    app_name = params["app_name"]
    app_code = params["app_code"]
    app_type = params.get("app_type", "APP")

    # Multi service expects X-Forwarded headers for URL construction
    create_headers = {**headers}

    body = {
        "appName": app_name,
        "appCode": app_code,
        "appType": app_type,
    }

    result = await client.post("/api/multi/application", headers=create_headers, json=body)

    if not result.success:
        return ToolResult(success=False, error=f"Failed to create application: {result.error}")

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
        ToolParameter(name="app_code", type="string", description="Unique code for the application (e.g. 'taskmanager')."),
        ToolParameter(name="app_type", type="string", description="Application type: 'APP' or 'SITE'.", required=False),
    ],
    execute=_create_application_execute,
)


# ── list_applications ───────────────────────────────────────────

async def _list_applications_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)

    result = await client.get("/api/security/applications", headers=headers)

    if not result.success:
        return ToolResult(success=False, error=f"Failed to list applications: {result.error}")

    apps = result.data if isinstance(result.data, list) else []
    lines = []
    for app in apps:
        name = app.get("appName", "?")
        code = app.get("appCode", "?")
        app_type = app.get("appType", "?")
        lines.append(f"- {name} (code={code}, type={app_type})")

    summary = f"Found {len(apps)} applications:\n" + "\n".join(lines)
    return ToolResult(success=True, data=apps, summary=summary)


list_applications = ToolDefinition(
    name="list_applications",
    description="List all applications accessible to the current user.",
    parameters=[],
    execute=_list_applications_execute,
)


# ── read_application ────────────────────────────────────────────

async def _read_application_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    app_code = params.get("app_code", context.get("app_code", ""))

    result = await client.get(
        "/api/ui/applications",
        headers=headers,
        params={"appCode": app_code},
    )

    if not result.success:
        return ToolResult(success=False, error=f"Failed to read application: {result.error}")

    return ToolResult(
        success=True,
        data=result.data,
        summary=f"Application '{app_code}':\n{json.dumps(result.data, indent=2, default=str)[:2000]}",
    )


read_application = ToolDefinition(
    name="read_application",
    description="Read an application's details by app code.",
    parameters=[
        ToolParameter(name="app_code", type="string", description="Application code to read.", required=False),
    ],
    execute=_read_application_execute,
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
    delete_application,
]
