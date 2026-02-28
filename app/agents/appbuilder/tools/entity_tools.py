"""Entity CRUD tools — connections, workflows, templates, fillers, uripaths.

These are the remaining entity types managed through /api/core/* and /api/ui/*
endpoints. Each follows the same CRUD pattern via SaasClient.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.tools.base import ToolDefinition, ToolResult, ToolParameter
from app.core.tools.http_client import SaasClient


def _get_client_and_headers(context: dict[str, Any]) -> tuple[SaasClient, dict[str, str]]:
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context["headers"]


# ── Generic CRUD helpers ────────────────────────────────────────

async def _generic_list(
    api_path: str, entity_name: str, params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    app_code = params.get("app_code", context.get("app_code", ""))

    result = await client.post(
        f"{api_path}/query",
        headers=headers,
        json={"page": 0, "size": 100, "condition": {"k": "appCode", "v": app_code}},
    )
    if not result.success:
        return ToolResult(success=False, error=f"Failed to list {entity_name}s: {result.error}")

    data = result.data
    items = data.get("content", []) if isinstance(data, dict) else []
    lines = [f"- {i.get('name', '?')} (id={i.get('id', '?')})" for i in items]

    return ToolResult(
        success=True,
        data=[{"name": i.get("name"), "id": i.get("id")} for i in items],
        summary=f"Found {len(items)} {entity_name}(s):\n" + "\n".join(lines),
    )


async def _generic_create(
    api_path: str, entity_name: str, params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    app_code = params.get("app_code", context.get("app_code", ""))

    body = {
        "name": params["name"],
        "appCode": app_code,
        "clientCode": context.get("client_code", ""),
        "definition": params.get("definition", {}),
    }

    result = await client.post(api_path, headers=headers, json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to create {entity_name}: {result.error}")

    created = result.data
    entity_id = created.get("id", "?") if isinstance(created, dict) else "?"
    return ToolResult(success=True, summary=f"Created {entity_name} '{params['name']}' (id={entity_id}).")


async def _generic_read(
    api_path: str, entity_name: str, params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    entity_id = params["id"]

    result = await client.get(f"{api_path}/{entity_id}", headers=headers)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to read {entity_name}: {result.error}")

    return ToolResult(
        success=True, data=result.data,
        summary=f"{entity_name}:\n{json.dumps(result.data, indent=2, default=str)[:2000]}",
    )


async def _generic_update(
    api_path: str, entity_name: str, params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    entity_id = params["id"]

    current = await client.get(f"{api_path}/{entity_id}", headers=headers)
    if not current.success:
        return ToolResult(success=False, error=f"Failed to read {entity_name}: {current.error}")

    entity_data = current.data
    if params.get("definition"):
        entity_data["definition"] = params["definition"]
    if params.get("name"):
        entity_data["name"] = params["name"]

    result = await client.put(f"{api_path}/{entity_id}", headers=headers, json=entity_data)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to update {entity_name}: {result.error}")

    return ToolResult(success=True, summary=f"Updated {entity_name} (id={entity_id}).")


async def _generic_delete(
    api_path: str, entity_name: str, params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    entity_id = params["id"]

    result = await client.delete(f"{api_path}/{entity_id}", headers=headers)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to delete {entity_name}: {result.error}")

    return ToolResult(success=True, summary=f"Deleted {entity_name} (id={entity_id}).")


# ── Entity tool factory ─────────────────────────────────────────

def _make_crud_tools(
    entity_name: str,
    api_path: str,
    description_prefix: str,
) -> list[ToolDefinition]:
    """Generate standard list/create/read/update/delete tools for an entity type."""

    async def list_exec(p: dict, c: dict) -> ToolResult:
        return await _generic_list(api_path, entity_name, p, c)

    async def create_exec(p: dict, c: dict) -> ToolResult:
        return await _generic_create(api_path, entity_name, p, c)

    async def read_exec(p: dict, c: dict) -> ToolResult:
        return await _generic_read(api_path, entity_name, p, c)

    async def update_exec(p: dict, c: dict) -> ToolResult:
        return await _generic_update(api_path, entity_name, p, c)

    async def delete_exec(p: dict, c: dict) -> ToolResult:
        return await _generic_delete(api_path, entity_name, p, c)

    return [
        ToolDefinition(
            name=f"list_{entity_name}s",
            description=f"List all {description_prefix} in an application.",
            parameters=[ToolParameter(name="app_code", type="string", description="Application code.", required=False)],
            execute=list_exec,
        ),
        ToolDefinition(
            name=f"create_{entity_name}",
            description=f"Create a new {description_prefix}.",
            parameters=[
                ToolParameter(name="name", type="string", description=f"{entity_name.title()} name."),
                ToolParameter(name="definition", type="object", description=f"{entity_name.title()} definition.", required=False),
                ToolParameter(name="app_code", type="string", description="Application code.", required=False),
            ],
            execute=create_exec,
        ),
        ToolDefinition(
            name=f"read_{entity_name}",
            description=f"Read a {description_prefix} by ID.",
            parameters=[ToolParameter(name="id", type="string", description=f"{entity_name.title()} ID.")],
            execute=read_exec,
        ),
        ToolDefinition(
            name=f"update_{entity_name}",
            description=f"Update a {description_prefix}.",
            parameters=[
                ToolParameter(name="id", type="string", description=f"{entity_name.title()} ID."),
                ToolParameter(name="name", type="string", description=f"New name.", required=False),
                ToolParameter(name="definition", type="object", description=f"New definition.", required=False),
            ],
            execute=update_exec,
        ),
        ToolDefinition(
            name=f"delete_{entity_name}",
            description=f"Delete a {description_prefix}.",
            parameters=[ToolParameter(name="id", type="string", description=f"{entity_name.title()} ID.")],
            execute=delete_exec,
        ),
    ]


# ── Generate entity tools ──────────────────────────────────────

CONNECTION_TOOLS = _make_crud_tools("connection", "/api/core/connections", "API connection")
WORKFLOW_TOOLS = _make_crud_tools("workflow", "/api/core/workflows", "workflow definition")
TEMPLATE_TOOLS = _make_crud_tools("template", "/api/core/templates", "template")
FILLER_TOOLS = _make_crud_tools("filler", "/api/ui/filler", "filler definition")
URIPATH_TOOLS = _make_crud_tools("uripath", "/api/ui/uripaths", "URI path route")

# ── Export all entity tools ─────────────────────────────────────

ENTITY_TOOLS: list[ToolDefinition] = (
    CONNECTION_TOOLS + WORKFLOW_TOOLS + TEMPLATE_TOOLS + FILLER_TOOLS + URIPATH_TOOLS
)
