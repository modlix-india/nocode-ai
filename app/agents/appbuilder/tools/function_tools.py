"""Function & Schema tools — deferred tools for logic management.

Functions use the KIRun DSL text format for LLM-friendly read/write
when the dsl_bridge is available, falling back to raw JSON otherwise.
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


def _get_dsl_bridge():
    """Try to import the DSL bridge. Returns None if kirun-py is not installed."""
    try:
        from app.agents.appbuilder.tools.dsl_bridge import get_dsl_bridge
        return get_dsl_bridge()
    except ImportError:
        return None


# ── create_function ──────────────────────────────────────────────


async def _execute_create_function(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    from app.agents.appbuilder.tools._shared import validate_name
    client, headers = _get_client_and_headers(context)
    app_code, err = _resolve_app_code(params, context)
    if err:
        return err

    name = params["name"]
    err = validate_name(name)
    if err:
        return err

    definition = params.get("definition", {})

    # If definition is DSL text, convert to JSON
    bridge = _get_dsl_bridge()
    if bridge and isinstance(definition, str) and bridge.is_dsl_text(definition):
        try:
            definition = await bridge.dsl_to_json(definition)
        except Exception as e:
            return ToolResult(success=False, error=f"DSL parse error: {e}")

    body = {
        "name": name,
        "appCode": app_code,
        "clientCode": context.get("client_code", ""),
        "namespace": params.get("namespace", ""),
        "definition": definition,
        "message": params.get("message", f"Created function '{name}'"),
    }
    result = await client.post("/api/ui/functions", headers=headers, json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to create function: {result.error}")
    created = result.data
    return ToolResult(
        success=True,
        summary=f"Created function '{name}' (id={created.get('id', '?')}).",
        result_tier=ResultTier.COMPACT,
    )


CREATE_FUNCTION = ToolDefinition(
    name="create_function",
    description="Create a KIRun function (UI or Core). Accepts definition as DSL text or JSON object.",
    parameters=[
        ToolParameter(name="name", type="string", description="Function name.", required=True),
        ToolParameter(name="namespace", type="string", description="Function namespace.", required=True),
        ToolParameter(name="definition", type="object", description="Function definition (JSON) or DSL text string.", required=True),
        ToolParameter(name="message", type="string", description="Change description.", required=True),
        ToolParameter(name="app_code", type="string", description="App code.", required=False),
    ],
    execute=_execute_create_function,
    is_deferred=True,
    search_hint="create KIRun function reusable logic namespace steps",
    result_tier=ResultTier.COMPACT,
)


# ── update_function ──────────────────────────────────────────────


async def _execute_update_function(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    from app.agents.appbuilder.tools._shared import save_entity
    client, headers = _get_client_and_headers(context)
    fn_id = params.get("id")
    if not fn_id:
        return ToolResult(success=False, error="id is required for function update.")

    current = await client.get(f"/api/ui/functions/{fn_id}", headers=headers)
    if not current.success:
        return ToolResult(success=False, error=f"Failed to read function: {current.error}")

    entity = current.data
    definition = params.get("definition")

    if definition:
        # If DSL text, convert to JSON
        bridge = _get_dsl_bridge()
        if bridge and isinstance(definition, str) and bridge.is_dsl_text(definition):
            try:
                definition = await bridge.dsl_to_json(definition)
            except Exception as e:
                return ToolResult(success=False, error=f"DSL parse error: {e}")
        entity["definition"] = definition

    entity["message"] = params.get("message", "Function update")

    result = await save_entity(
        client, "/api/ui/functions", fn_id, entity, headers, context.get("client_code", ""),
    )
    if not result.success:
        return result
    return ToolResult(success=True, summary=f"Updated function (id={fn_id}).", result_tier=ResultTier.COMPACT)


UPDATE_FUNCTION = ToolDefinition(
    name="update_function",
    description="Update a KIRun function definition. Accepts definition as DSL text or JSON.",
    parameters=[
        ToolParameter(name="id", type="string", description="Function ID.", required=True),
        ToolParameter(name="definition", type="object", description="New function definition (JSON or DSL text).", required=True),
        ToolParameter(name="message", type="string", description="Change description.", required=True),
    ],
    execute=_execute_update_function,
    is_deferred=True,
    search_hint="update function steps parameters events KIRun",
    result_tier=ResultTier.COMPACT,
)


# ── read_function_steps ──────────────────────────────────────────


async def _execute_read_function_steps(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Read function steps, optionally in DSL format."""
    client, headers = _get_client_and_headers(context)
    fn_id = params.get("id")
    if not fn_id:
        return ToolResult(success=False, error="id is required.")

    result = await client.get(f"/api/ui/functions/{fn_id}", headers=headers)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to read function: {result.error}")

    fn_data = result.data
    definition = fn_data.get("definition", {})

    # Try DSL conversion
    bridge = _get_dsl_bridge()
    if bridge:
        try:
            dsl_text = await bridge.json_to_dsl(definition)
            return ToolResult(
                success=True,
                summary=f"Function '{fn_data.get('name', '?')}' (DSL format):\n\n{dsl_text}",
                result_tier=ResultTier.STANDARD,
            )
        except Exception:
            pass  # Fall back to JSON summary

    # Fallback: JSON summary of steps
    steps = definition.get("steps", {})
    step_names = params.get("step_names", [])
    if step_names:
        steps = {k: v for k, v in steps.items() if k in step_names}

    compact = {}
    for sname, sdef in steps.items():
        compact[sname] = {
            "namespace": sdef.get("namespace", "?"),
            "name": sdef.get("name", "?"),
            "dependentSteps": sdef.get("dependentSteps", []),
            "parameterMapKeys": list(sdef.get("parameterMap", {}).keys()),
        }

    return ToolResult(
        success=True,
        summary=f"Function '{fn_data.get('name', '?')}' ({len(compact)} steps):\n{json.dumps(compact, indent=2)}",
        result_tier=ResultTier.STANDARD,
    )


READ_FUNCTION_STEPS = ToolDefinition(
    name="read_function_steps",
    description="Read function steps in DSL text format (or JSON fallback). Optionally filter by step names.",
    parameters=[
        ToolParameter(name="id", type="string", description="Function ID.", required=True),
        ToolParameter(
            name="step_names",
            type="array",
            description="Optional: specific step names to read.",
            required=False,
            items={"type": "string"},
        ),
    ],
    execute=_execute_read_function_steps,
    is_deferred=True,
    search_hint="read function step details DSL text format KIRun",
    result_tier=ResultTier.STANDARD,
)


# ── create_schema ────────────────────────────────────────────────


async def _execute_create_schema(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
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
        "message": params.get("message", f"Created schema '{name}'"),
    }
    result = await client.post("/api/ui/schemas", headers=headers, json=body)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to create schema: {result.error}")
    created = result.data
    return ToolResult(
        success=True,
        summary=f"Created schema '{name}' (id={created.get('id', '?')}).",
        result_tier=ResultTier.COMPACT,
    )


CREATE_SCHEMA = ToolDefinition(
    name="create_schema",
    description="Create a data schema definition (type structure for function parameters/events).",
    parameters=[
        ToolParameter(name="name", type="string", description="Schema name.", required=True),
        ToolParameter(name="definition", type="object", description="Schema definition.", required=True),
        ToolParameter(name="message", type="string", description="Change description.", required=True),
        ToolParameter(name="app_code", type="string", description="App code.", required=False),
    ],
    execute=_execute_create_schema,
    is_deferred=True,
    search_hint="create schema data model type definition",
    result_tier=ResultTier.COMPACT,
)


# ── update_schema ────────────────────────────────────────────────


async def _execute_update_schema(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    from app.agents.appbuilder.tools._shared import save_entity
    client, headers = _get_client_and_headers(context)
    schema_id = params.get("id")
    if not schema_id:
        return ToolResult(success=False, error="id is required.")
    current = await client.get(f"/api/ui/schemas/{schema_id}", headers=headers)
    if not current.success:
        return ToolResult(success=False, error=f"Failed to read schema: {current.error}")
    entity = current.data
    if params.get("definition"):
        entity["definition"] = params["definition"]
    entity["message"] = params.get("message", "Schema update")
    result = await save_entity(
        client, "/api/ui/schemas", schema_id, entity, headers, context.get("client_code", ""),
    )
    if not result.success:
        return result
    return ToolResult(success=True, summary=f"Updated schema (id={schema_id}).", result_tier=ResultTier.COMPACT)


UPDATE_SCHEMA = ToolDefinition(
    name="update_schema",
    description="Update a data schema definition.",
    parameters=[
        ToolParameter(name="id", type="string", description="Schema ID.", required=True),
        ToolParameter(name="definition", type="object", description="Updated definition.", required=True),
        ToolParameter(name="message", type="string", description="Change description.", required=True),
    ],
    execute=_execute_update_schema,
    is_deferred=True,
    search_hint="update schema properties fields type",
    result_tier=ResultTier.COMPACT,
)


# ── Exports ──────────────────────────────────────────────────────

FUNCTION_TOOLS: list[ToolDefinition] = [
    CREATE_FUNCTION,
    UPDATE_FUNCTION,
    READ_FUNCTION_STEPS,
    CREATE_SCHEMA,
    UPDATE_SCHEMA,
]
