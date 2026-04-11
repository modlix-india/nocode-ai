"""Connection, Template, and URIPath tools — deferred tools for data/routing.

These entity types are small enough for whole-document CRUD operations.
Each tool wraps the generic CRUD pattern with type-specific parameters.
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


def _resolve_app_code(params: dict[str, Any], context: dict[str, Any]) -> tuple[str, ToolResult | None]:
    app_code = params.get("app_code") or context.get("app_code", "")
    if not app_code:
        return "", ToolResult(success=False, error="No appCode set.")
    return app_code, None


async def _generic_create(
    api_path: str, display: str, params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
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
        "message": params.get("message", f"Created {display} '{name}'"),
    }
    if params.get("namespace"):
        body["namespace"] = params["namespace"]
    result = await client.post(api_path, headers=headers, json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to create {display}: {result.error}")
    created = result.data
    return ToolResult(
        success=True,
        summary=f"Created {display} '{name}' (id={created.get('id', '?')}).",
        result_tier=ResultTier.COMPACT,
    )


async def _generic_update(
    api_path: str, display: str, params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    from app.agents.appbuilder.tools._shared import save_entity
    client, headers = _get_client_and_headers(context)
    entity_id = params.get("id")
    if not entity_id:
        return ToolResult(success=False, error="id is required for update.")
    current = await client.get(f"{api_path}/{entity_id}", headers=headers)
    if not current.success:
        return ToolResult(success=False, error=f"Failed to read {display}: {current.error}")
    entity = current.data
    if params.get("definition"):
        entity["definition"] = params["definition"]
    if params.get("name"):
        entity["name"] = params["name"]
    entity["message"] = params.get("message", f"Updated {display}")
    result = await save_entity(client, api_path, entity_id, entity, headers, context.get("client_code", ""))
    if not result.success:
        return result
    return ToolResult(success=True, summary=f"Updated {display} (id={entity_id}).", result_tier=ResultTier.COMPACT)


# ── Connection ───────────────────────────────────────────────────

async def _exec_create_connection(p: dict, c: dict) -> ToolResult:
    return await _generic_create("/api/core/connections", "connection", p, c)

async def _exec_update_connection(p: dict, c: dict) -> ToolResult:
    return await _generic_update("/api/core/connections", "connection", p, c)

CREATE_CONNECTION = ToolDefinition(
    name="create_connection",
    description="Create an API connection configuration (endpoints, auth, headers).",
    parameters=[
        ToolParameter(name="name", type="string", description="Connection name.", required=True),
        ToolParameter(name="definition", type="object", description="Connection definition.", required=True),
        ToolParameter(name="message", type="string", description="Change description.", required=True),
        ToolParameter(name="app_code", type="string", description="App code.", required=False),
    ],
    execute=_exec_create_connection,
    is_deferred=True,
    search_hint="create API connection endpoint auth headers",
    result_tier=ResultTier.COMPACT,
)

UPDATE_CONNECTION = ToolDefinition(
    name="update_connection",
    description="Update an API connection configuration.",
    parameters=[
        ToolParameter(name="id", type="string", description="Connection ID.", required=True),
        ToolParameter(name="definition", type="object", description="Updated definition.", required=True),
        ToolParameter(name="message", type="string", description="Change description.", required=True),
    ],
    execute=_exec_update_connection,
    is_deferred=True,
    search_hint="update API connection endpoint configuration",
    result_tier=ResultTier.COMPACT,
)


# ── URIPath ──────────────────────────────────────────────────────

async def _exec_create_uripath(p: dict, c: dict) -> ToolResult:
    return await _generic_create("/api/ui/uripaths", "URI path", p, c)

async def _exec_update_uripath(p: dict, c: dict) -> ToolResult:
    return await _generic_update("/api/ui/uripaths", "URI path", p, c)

CREATE_URIPATH = ToolDefinition(
    name="create_uripath",
    description="Create a URL route/path definition for backend API routing.",
    parameters=[
        ToolParameter(name="name", type="string", description="URI path name (URL pattern).", required=True),
        ToolParameter(name="definition", type="object", description="Path definitions (HTTP method → handler).", required=True),
        ToolParameter(name="message", type="string", description="Change description.", required=True),
        ToolParameter(name="app_code", type="string", description="App code.", required=False),
    ],
    execute=_exec_create_uripath,
    is_deferred=True,
    search_hint="create URL route path parameter backend",
    result_tier=ResultTier.COMPACT,
)

UPDATE_URIPATH = ToolDefinition(
    name="update_uripath",
    description="Update a URL route/path definition.",
    parameters=[
        ToolParameter(name="id", type="string", description="URIPath ID.", required=True),
        ToolParameter(name="definition", type="object", description="Updated path definitions.", required=True),
        ToolParameter(name="message", type="string", description="Change description.", required=True),
    ],
    execute=_exec_update_uripath,
    is_deferred=True,
    search_hint="update URL route path definition handler",
    result_tier=ResultTier.COMPACT,
)


# ── Template ─────────────────────────────────────────────────────

async def _exec_create_template(p: dict, c: dict) -> ToolResult:
    return await _generic_create("/api/core/templates", "template", p, c)

async def _exec_update_template(p: dict, c: dict) -> ToolResult:
    return await _generic_update("/api/core/templates", "template", p, c)

MANAGE_TEMPLATE = ToolDefinition(
    name="manage_template",
    description="Create or update a message/email template. Use with action='create' (requires name+definition) or action='update' (requires id+definition).",
    parameters=[
        ToolParameter(name="action", type="string", description="'create' or 'update'.", required=True, enum=["create", "update"]),
        ToolParameter(name="name", type="string", description="Template name (for create).", required=False),
        ToolParameter(name="id", type="string", description="Template ID (for update).", required=False),
        ToolParameter(name="definition", type="object", description="Template definition.", required=True),
        ToolParameter(name="message", type="string", description="Change description.", required=True),
        ToolParameter(name="app_code", type="string", description="App code.", required=False),
    ],
    execute=lambda p, c: _exec_create_template(p, c) if p.get("action") == "create" else _exec_update_template(p, c),
    is_deferred=True,
    search_hint="create update email message template",
    result_tier=ResultTier.COMPACT,
)


# ── Exports ──────────────────────────────────────────────────────

DATA_TOOLS: list[ToolDefinition] = [
    CREATE_CONNECTION,
    UPDATE_CONNECTION,
    CREATE_URIPATH,
    UPDATE_URIPATH,
    MANAGE_TEMPLATE,
]
