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
    from app.agents.appbuilder.tools._shared import require_app_code

    client, headers = _get_client_and_headers(context)
    app_code = params.get("app_code") or context.get("app_code", "")
    if not app_code:
        _, err = require_app_code(context)
        return err

    result = await client.get(
        api_path,
        headers=headers,
        params={"page": 0, "size": 1000, "appCode": app_code},
    )
    if not result.success:
        return ToolResult(success=False, error=f"Failed to list {entity_name}s: {result.error}")

    data = result.data
    items = data.get("content", []) if isinstance(data, dict) else []
    lines = [f"- {i.get('name', '?')} (id={i.get('id', '?')}, v{i.get('version', '?')})" for i in items]

    return ToolResult(
        success=True,
        data=[{"name": i.get("name"), "id": i.get("id"), "version": i.get("version")} for i in items],
        summary=f"Found {len(items)} {entity_name}(s):\n" + "\n".join(lines),
    )


async def _generic_create(
    api_path: str, entity_name: str, params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
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

    result = await client.post(api_path, headers=headers, json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to create {entity_name}: {result.error}")

    created = result.data
    entity_id = created.get("id", "?") if isinstance(created, dict) else "?"
    return ToolResult(success=True, summary=f"Created {entity_name} '{name}' (id={entity_id}).")


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
    if params.get("title"):
        entity_data["title"] = params["title"]
    if params.get("description"):
        entity_data["description"] = params["description"]
    entity_data["message"] = params["message"]

    from app.agents.appbuilder.tools._shared import save_entity
    result = await save_entity(client, api_path, entity_id, entity_data, headers, context.get("client_code", ""))
    if not result.success:
        return result

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
            display_name=f"List {entity_name.title()}s",
            description=f"List all {description_prefix} in an application.",
            parameters=[ToolParameter(name="app_code", type="string", description="Application code.", required=False)],
            execute=list_exec,
        ),
        ToolDefinition(
            name=f"create_{entity_name}",
            display_name=f"Create {entity_name.title()}",
            description=f"Create a new {description_prefix}.",
            parameters=[
                ToolParameter(name="name", type="string", description=f"{entity_name.title()} name (letters only)."),
                ToolParameter(name="definition", type="object", description=f"{entity_name.title()} definition.", required=False),
                ToolParameter(name="title", type="string", description=f"{entity_name.title()} title.", required=False),
                ToolParameter(name="description", type="string", description=f"{entity_name.title()} description.", required=False),
                ToolParameter(name="message", type="string", description="Commit message (10–15 words) describing what was changed."),
                ToolParameter(name="app_code", type="string", description="Application code.", required=False),
            ],
            execute=create_exec,
        ),
        ToolDefinition(
            name=f"read_{entity_name}",
            display_name=f"Read {entity_name.title()}",
            description=f"Read a {description_prefix} by ID.",
            parameters=[ToolParameter(name="id", type="string", description=f"{entity_name.title()} ID.")],
            execute=read_exec,
        ),
        ToolDefinition(
            name=f"update_{entity_name}",
            display_name=f"Update {entity_name.title()}",
            description=f"Update a {description_prefix}.",
            parameters=[
                ToolParameter(name="id", type="string", description=f"{entity_name.title()} ID."),
                ToolParameter(name="name", type="string", description="New name.", required=False),
                ToolParameter(name="definition", type="object", description="New definition.", required=False),
                ToolParameter(name="title", type="string", description=f"{entity_name.title()} title.", required=False),
                ToolParameter(name="description", type="string", description=f"{entity_name.title()} description.", required=False),
                ToolParameter(name="message", type="string", description="Commit message (10–15 words) describing what was changed."),
            ],
            execute=update_exec,
        ),
        ToolDefinition(
            name=f"delete_{entity_name}",
            display_name=f"Delete {entity_name.title()}",
            description=f"Delete a {description_prefix}. If the object is inherited (owned by another client), this removes your override and resets to the inherited version.",
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
