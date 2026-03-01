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
    from app.agents.appbuilder.tools._shared import require_app_code

    client, headers = _get_client_and_headers(context)
    app_code = params.get("app_code") or context.get("app_code", "")
    if not app_code:
        _, err = require_app_code(context)
        return err

    result = await client.get(
        "/api/ui/functions",
        headers=headers,
        params={"page": 0, "size": 1000, "appCode": app_code},
    )
    if not result.success:
        return ToolResult(success=False, error=f"Failed to list functions: {result.error}")

    data = result.data
    functions = data.get("content", []) if isinstance(data, dict) else []
    lines = [f"- {f.get('name', '?')}.{f.get('namespace', '?')} (id={f.get('id', '?')}, v{f.get('version', '?')})" for f in functions]

    return ToolResult(
        success=True,
        data=[{"name": f.get("name"), "namespace": f.get("namespace"), "id": f.get("id"), "version": f.get("version")} for f in functions],
        summary=f"Found {len(functions)} functions:\n" + "\n".join(lines),
    )


async def _create_function_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
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
        "namespace": params.get("namespace", ""),
        "appCode": app_code,
        "clientCode": context.get("client_code", ""),
        "definition": params["definition"],
    }
    if params.get("title"):
        body["title"] = params["title"]
    if params.get("description"):
        body["description"] = params["description"]
    body["message"] = params["message"]

    result = await client.post("/api/ui/functions", headers=headers, json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to create function: {result.error}")

    created = result.data
    fn_id = created.get("id", "?") if isinstance(created, dict) else "?"
    return ToolResult(success=True, summary=f"Created function '{name}' (id={fn_id}).")


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
    if params.get("title"):
        fn_data["title"] = params["title"]
    if params.get("description"):
        fn_data["description"] = params["description"]
    fn_data["message"] = params["message"]

    from app.agents.appbuilder.tools._shared import save_entity
    result = await save_entity(client, "/api/ui/functions", function_id, fn_data, headers, context.get("client_code", ""))
    if not result.success:
        return result

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
        ToolParameter(name="name", type="string", description="Function name (letters only)."),
        ToolParameter(name="namespace", type="string", description="Function namespace.", required=False),
        ToolParameter(name="definition", type="object", description="KIRun function definition with steps, events, etc."),
        ToolParameter(name="title", type="string", description="Function title.", required=False),
        ToolParameter(name="description", type="string", description="Function description.", required=False),
        ToolParameter(name="message", type="string", description="Commit message (10–15 words) describing what was changed."),
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
        ToolParameter(name="title", type="string", description="Function title.", required=False),
        ToolParameter(name="description", type="string", description="Function description.", required=False),
        ToolParameter(name="message", type="string", description="Commit message (10–15 words) describing what was changed."),
    ],
    execute=_update_function_execute,
)

search_builtin_functions = ToolDefinition(
    name="search_builtin_functions",
    description=(
        "Search KIRun CORE/SYSTEM builtin functions by name (System.*, UIEngine.*, etc.). "
        "Returns namespace.name list. Use when building reusable KIRun function definitions "
        "(stored in /api/ui/functions) to find available step functions like "
        "SetStore, Navigate, CallRequest, System.If, System.Loop.RangeLoop, etc. "
        "NOTE: Page event functions (stored inline in page.eventFunctions) also call these "
        "same system functions as steps — use this to discover them. "
        "Follow up with get_kirun_function_signature to get exact parameter names and output events."
    ),
    parameters=[ToolParameter(name="query", type="string", description="Search query (e.g. 'SetStore', 'Navigate', 'HTTPService', 'String').")],
    execute=_search_builtin_functions_execute,
)


# ── KIRun function signature ────────────────────────────────────

async def _get_kirun_function_signature_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    namespace = params["namespace"]
    name = params["name"]

    result = await client.get(
        f"/api/core/functions/{namespace}/{name}",
        headers=headers,
    )
    if not result.success:
        return ToolResult(success=False, error=f"Failed to get function signature: {result.error}")

    fn = result.data
    if not isinstance(fn, dict):
        return ToolResult(success=False, error="Unexpected response format.")

    # Extract only the parts needed to build a step definition
    props = fn.get("properties", {})
    events = fn.get("events", {})

    # Summarise input ports
    input_lines = []
    for port_name, port_def in props.items():
        ptype = port_def.get("type", "?")
        required = "" if port_def.get("required") else " (optional)"
        input_lines.append(f"  {port_name}: {ptype}{required}")

    # Summarise output events and their ports
    event_lines = []
    for event_name, event_def in events.items():
        out_ports = event_def.get("ports", {})
        if out_ports:
            port_summary = ", ".join(f"{p}: {d.get('type','?')}" for p, d in out_ports.items())
            event_lines.append(f"  {event_name} → {port_summary}")
        else:
            event_lines.append(f"  {event_name} (no output ports)")

    summary_lines = [
        f"KIRun function: {namespace}.{name}",
        "",
        "INPUT PORTS (use as step 'properties' keys):",
    ] + (input_lines or ["  (none)"]) + [
        "",
        "OUTPUT EVENTS (use as dependsOn eventName, reference outputs with ${stepName.output.portName}):",
    ] + (event_lines or ["  (none)"])

    return ToolResult(
        success=True,
        data={"properties": props, "events": events},
        summary="\n".join(summary_lines),
    )


get_kirun_function_signature = ToolDefinition(
    name="get_kirun_function_signature",
    description=(
        "Get the full input/output signature of a KIRun CORE/SYSTEM builtin function. "
        "Returns parameter (input port) names+types and output event names+port names. "
        "Use this BEFORE writing a step in a reusable KIRun function definition "
        "(i.e. functions stored via /api/ui/functions) to know exactly what parameterMap "
        "keys to set and how to reference step outputs in dependentStatements. "
        "IMPORTANT: This is for CORE/SYSTEM functions (System.*, UIEngine.*, etc.) "
        "that are called AS STEPS — NOT for page event functions. "
        "Page event functions are inline step sequences defined in page.eventFunctions "
        "and do not have a formal parameter signature. "
        "Workflow: search_builtin_functions → get_kirun_function_signature → write step definition."
    ),
    parameters=[
        ToolParameter(name="namespace", type="string", description="Function namespace (e.g. 'System', 'System.HTTPService', 'UIEngine')."),
        ToolParameter(name="name", type="string", description="Function name (e.g. 'If', 'CallRequest', 'SetStore')."),
    ],
    execute=_get_kirun_function_signature_execute,
)


# ── Schema CRUD ─────────────────────────────────────────────────

async def _list_schemas_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    from app.agents.appbuilder.tools._shared import require_app_code

    client, headers = _get_client_and_headers(context)
    app_code = params.get("app_code") or context.get("app_code", "")
    if not app_code:
        _, err = require_app_code(context)
        return err

    result = await client.get(
        "/api/ui/schemas",
        headers=headers,
        params={"page": 0, "size": 1000, "appCode": app_code},
    )
    if not result.success:
        return ToolResult(success=False, error=f"Failed to list schemas: {result.error}")

    data = result.data
    schemas = data.get("content", []) if isinstance(data, dict) else []
    lines = [f"- {s.get('name', '?')} (id={s.get('id', '?')}, v{s.get('version', '?')})" for s in schemas]

    return ToolResult(
        success=True,
        data=[{"name": s.get("name"), "id": s.get("id"), "version": s.get("version")} for s in schemas],
        summary=f"Found {len(schemas)} schemas:\n" + "\n".join(lines),
    )


async def _create_schema_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
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
        "definition": params["definition"],
    }
    if params.get("title"):
        body["title"] = params["title"]
    if params.get("description"):
        body["description"] = params["description"]
    body["message"] = params["message"]

    result = await client.post("/api/ui/schemas", headers=headers, json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to create schema: {result.error}")

    created = result.data
    schema_id = created.get("id", "?") if isinstance(created, dict) else "?"
    return ToolResult(success=True, summary=f"Created schema '{name}' (id={schema_id}).")


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
    if params.get("title"):
        schema_data["title"] = params["title"]
    if params.get("description"):
        schema_data["description"] = params["description"]
    schema_data["message"] = params["message"]

    from app.agents.appbuilder.tools._shared import save_entity
    result = await save_entity(client, "/api/ui/schemas", schema_id, schema_data, headers, context.get("client_code", ""))
    if not result.success:
        return result

    return ToolResult(success=True, summary=f"Updated schema (id={schema_id}).")


list_schemas = ToolDefinition(
    name="list_schemas", description="List all data schemas in an application.",
    parameters=[ToolParameter(name="app_code", type="string", description="Application code.", required=False)],
    execute=_list_schemas_execute,
)

create_schema = ToolDefinition(
    name="create_schema", description="Create a new data schema definition.",
    parameters=[
        ToolParameter(name="name", type="string", description="Schema name (letters only)."),
        ToolParameter(name="definition", type="object", description="Schema definition object."),
        ToolParameter(name="title", type="string", description="Schema title.", required=False),
        ToolParameter(name="description", type="string", description="Schema description.", required=False),
        ToolParameter(name="message", type="string", description="Commit message (10–15 words) describing what was changed."),
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
        ToolParameter(name="title", type="string", description="Schema title.", required=False),
        ToolParameter(name="description", type="string", description="Schema description.", required=False),
        ToolParameter(name="message", type="string", description="Commit message (10–15 words) describing what was changed."),
    ],
    execute=_update_schema_execute,
)


# ── Export ───────────────────────────────────────────────────────

FUNCTION_TOOLS: list[ToolDefinition] = [
    list_functions, create_function, read_function, update_function,
    search_builtin_functions, get_kirun_function_signature,
]

SCHEMA_TOOLS: list[ToolDefinition] = [
    list_schemas, create_schema, read_schema, update_schema,
]
