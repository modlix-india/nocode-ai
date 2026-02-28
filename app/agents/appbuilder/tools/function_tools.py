"""Function and schema management tools.

Functions: /api/ui/functions — reusable KIRun function definitions.
Schemas: /api/ui/schemas — data schema definitions.
Builtin function search: /api/core/functions/repositoryFilter
"""

from __future__ import annotations

import json
from typing import Any

from app.core.tools.base import ToolDefinition, ToolResult, ToolParameter
from app.core.tools.http_client import SaasClient


def _get_client_and_headers(context: dict[str, Any]) -> tuple[SaasClient, dict[str, str]]:
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context["headers"]


# ── Function CRUD ───────────────────────────────────────────────

async def _list_functions_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    app_code = params.get("app_code", context.get("app_code", ""))

    result = await client.post(
        "/api/ui/functions/query",
        headers=headers,
        json={"page": 0, "size": 100, "condition": {"k": "appCode", "v": app_code}},
    )
    if not result.success:
        return ToolResult(success=False, error=f"Failed to list functions: {result.error}")

    data = result.data
    functions = data.get("content", []) if isinstance(data, dict) else []
    lines = [f"- {f.get('name', '?')}.{f.get('namespace', '?')} (id={f.get('id', '?')})" for f in functions]

    return ToolResult(
        success=True,
        data=[{"name": f.get("name"), "namespace": f.get("namespace"), "id": f.get("id")} for f in functions],
        summary=f"Found {len(functions)} functions:\n" + "\n".join(lines),
    )


async def _create_function_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    app_code = params.get("app_code", context.get("app_code", ""))

    body = {
        "name": params["name"],
        "namespace": params.get("namespace", ""),
        "appCode": app_code,
        "clientCode": context.get("client_code", ""),
        "definition": params["definition"],
    }

    result = await client.post("/api/ui/functions", headers=headers, json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to create function: {result.error}")

    created = result.data
    fn_id = created.get("id", "?") if isinstance(created, dict) else "?"
    return ToolResult(success=True, summary=f"Created function '{params['name']}' (id={fn_id}).")


async def _read_function_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    function_id = params["function_id"]

    result = await client.get(f"/api/ui/functions/{function_id}", headers=headers)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to read function: {result.error}")

    return ToolResult(
        success=True, data=result.data,
        summary=f"Function:\n{json.dumps(result.data, indent=2, default=str)[:2000]}",
    )


async def _update_function_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    function_id = params["function_id"]

    current = await client.get(f"/api/ui/functions/{function_id}", headers=headers)
    if not current.success:
        return ToolResult(success=False, error=f"Failed to read function: {current.error}")

    fn_data = current.data
    if params.get("definition"):
        fn_data["definition"] = params["definition"]
    if params.get("name"):
        fn_data["name"] = params["name"]

    result = await client.put(f"/api/ui/functions/{function_id}", headers=headers, json=fn_data)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to update function: {result.error}")

    return ToolResult(success=True, summary=f"Updated function (id={function_id}).")


async def _search_builtin_functions_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    query = params["query"]

    result = await client.get(
        "/api/core/functions/repositoryFilter",
        headers=headers,
        params={"filter": query, "page": 0, "size": 20},
    )
    if not result.success:
        return ToolResult(success=False, error=f"Failed to search functions: {result.error}")

    data = result.data
    functions = data.get("content", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    lines = [f"- {f.get('namespace', '')}.{f.get('name', '?')}" for f in functions[:20]]

    return ToolResult(
        success=True, data=functions,
        summary=f"Found {len(functions)} builtin functions matching '{query}':\n" + "\n".join(lines),
    )


list_functions = ToolDefinition(
    name="list_functions", description="List all custom functions in an application.",
    parameters=[ToolParameter(name="app_code", type="string", description="Application code.", required=False)],
    execute=_list_functions_execute,
)

create_function = ToolDefinition(
    name="create_function", description="Create a new reusable KIRun function definition.",
    parameters=[
        ToolParameter(name="name", type="string", description="Function name."),
        ToolParameter(name="namespace", type="string", description="Function namespace.", required=False),
        ToolParameter(name="definition", type="object", description="KIRun function definition with steps, events, etc."),
        ToolParameter(name="app_code", type="string", description="Application code.", required=False),
    ],
    execute=_create_function_execute,
)

read_function = ToolDefinition(
    name="read_function", description="Read a function's full definition.",
    parameters=[ToolParameter(name="function_id", type="string", description="Function ID.")],
    execute=_read_function_execute,
)

update_function = ToolDefinition(
    name="update_function", description="Update a function's definition.",
    parameters=[
        ToolParameter(name="function_id", type="string", description="Function ID."),
        ToolParameter(name="name", type="string", description="New function name.", required=False),
        ToolParameter(name="definition", type="object", description="New KIRun function definition.", required=False),
    ],
    execute=_update_function_execute,
)

search_builtin_functions = ToolDefinition(
    name="search_builtin_functions",
    description="Search KIRun builtin/system functions by name. Use to find available functions like SetStore, Navigate, CallRequest, etc.",
    parameters=[ToolParameter(name="query", type="string", description="Search query (e.g. 'SetStore', 'Navigate', 'String').")],
    execute=_search_builtin_functions_execute,
)


# ── Schema CRUD ─────────────────────────────────────────────────

async def _list_schemas_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    app_code = params.get("app_code", context.get("app_code", ""))

    result = await client.post(
        "/api/ui/schemas/query",
        headers=headers,
        json={"page": 0, "size": 100, "condition": {"k": "appCode", "v": app_code}},
    )
    if not result.success:
        return ToolResult(success=False, error=f"Failed to list schemas: {result.error}")

    data = result.data
    schemas = data.get("content", []) if isinstance(data, dict) else []
    lines = [f"- {s.get('name', '?')} (id={s.get('id', '?')})" for s in schemas]

    return ToolResult(
        success=True,
        data=[{"name": s.get("name"), "id": s.get("id")} for s in schemas],
        summary=f"Found {len(schemas)} schemas:\n" + "\n".join(lines),
    )


async def _create_schema_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    app_code = params.get("app_code", context.get("app_code", ""))

    body = {
        "name": params["name"],
        "appCode": app_code,
        "clientCode": context.get("client_code", ""),
        "definition": params["definition"],
    }

    result = await client.post("/api/ui/schemas", headers=headers, json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to create schema: {result.error}")

    created = result.data
    schema_id = created.get("id", "?") if isinstance(created, dict) else "?"
    return ToolResult(success=True, summary=f"Created schema '{params['name']}' (id={schema_id}).")


async def _read_schema_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    schema_id = params["schema_id"]

    result = await client.get(f"/api/ui/schemas/{schema_id}", headers=headers)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to read schema: {result.error}")

    return ToolResult(
        success=True, data=result.data,
        summary=f"Schema:\n{json.dumps(result.data, indent=2, default=str)[:2000]}",
    )


async def _update_schema_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    schema_id = params["schema_id"]

    current = await client.get(f"/api/ui/schemas/{schema_id}", headers=headers)
    if not current.success:
        return ToolResult(success=False, error=f"Failed to read schema: {current.error}")

    schema_data = current.data
    if params.get("definition"):
        schema_data["definition"] = params["definition"]
    if params.get("name"):
        schema_data["name"] = params["name"]

    result = await client.put(f"/api/ui/schemas/{schema_id}", headers=headers, json=schema_data)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to update schema: {result.error}")

    return ToolResult(success=True, summary=f"Updated schema (id={schema_id}).")


list_schemas = ToolDefinition(
    name="list_schemas", description="List all data schemas in an application.",
    parameters=[ToolParameter(name="app_code", type="string", description="Application code.", required=False)],
    execute=_list_schemas_execute,
)

create_schema = ToolDefinition(
    name="create_schema", description="Create a new data schema definition.",
    parameters=[
        ToolParameter(name="name", type="string", description="Schema name."),
        ToolParameter(name="definition", type="object", description="Schema definition object."),
        ToolParameter(name="app_code", type="string", description="Application code.", required=False),
    ],
    execute=_create_schema_execute,
)

read_schema = ToolDefinition(
    name="read_schema", description="Read a schema definition.",
    parameters=[ToolParameter(name="schema_id", type="string", description="Schema ID.")],
    execute=_read_schema_execute,
)

update_schema = ToolDefinition(
    name="update_schema", description="Update a schema definition.",
    parameters=[
        ToolParameter(name="schema_id", type="string", description="Schema ID."),
        ToolParameter(name="name", type="string", description="New schema name.", required=False),
        ToolParameter(name="definition", type="object", description="New schema definition.", required=False),
    ],
    execute=_update_schema_execute,
)


# ── Export ───────────────────────────────────────────────────────

FUNCTION_TOOLS: list[ToolDefinition] = [
    list_functions, create_function, read_function, update_function, search_builtin_functions,
]

SCHEMA_TOOLS: list[ToolDefinition] = [
    list_schemas, create_schema, read_schema, update_schema,
]
