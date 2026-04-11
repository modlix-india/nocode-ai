"""Remote function repository tools — discover and read server-side functions.

These use the same Core service endpoints that the browser's
RemoteRepository uses:
  - GET /api/core/function/repositoryFilter?name=<query>  (list/search)
  - GET /api/core/function/repositoryFind?namespace=X&name=Y  (read signature)

The agent uses these when writing page event functions or UI functions
that call server (Core) functions — it needs to know what server
functions exist and what parameters they accept.
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


# ── list_remote_functions ────────────────────────────────────────


async def _execute_list_remote_functions(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    """List available server-side (Core) functions."""
    client, headers = _get_client_and_headers(context)
    query = params.get("query", "")

    api_params: dict[str, str] = {}
    if query:
        api_params["name"] = query

    result = await client.get(
        "/api/core/function/repositoryFilter",
        headers=headers,
        params=api_params,
    )
    if not result.success:
        return ToolResult(
            success=False,
            error=f"Failed to query remote functions: {result.error}",
        )

    functions = result.data
    if not functions:
        return ToolResult(
            success=True,
            summary=f"No server functions found matching '{query}'." if query else "No server functions available.",
            result_tier=ResultTier.COMPACT,
        )

    # Format as a compact list
    if isinstance(functions, list):
        lines = []
        for fn in functions[:50]:  # Cap at 50 results
            if isinstance(fn, dict):
                ns = fn.get("namespace", "?")
                name = fn.get("name", "?")
                lines.append(f"- {ns}.{name}")
            elif isinstance(fn, str):
                lines.append(f"- {fn}")
        summary = f"Found {len(functions)} server function(s):\n" + "\n".join(lines)
        if len(functions) > 50:
            summary += f"\n... and {len(functions) - 50} more"
    else:
        summary = json.dumps(functions, indent=2, default=str)

    return ToolResult(
        success=True,
        data=functions,
        summary=summary,
        result_tier=ResultTier.STANDARD,
    )


LIST_REMOTE_FUNCTIONS = ToolDefinition(
    name="list_remote_functions",
    description=(
        "List available server-side (Core) functions. Use when building page events "
        "or UI functions that need to call server functions. "
        "Optionally filter by name substring."
    ),
    parameters=[
        ToolParameter(
            name="query",
            type="string",
            description="Optional name filter (substring match).",
            required=False,
        ),
    ],
    execute=_execute_list_remote_functions,
    is_deferred=True,
    search_hint="list available server core remote functions repository",
    result_tier=ResultTier.STANDARD,
)


# ── read_remote_function ─────────────────────────────────────────


async def _execute_read_remote_function(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    """Read a remote function's signature (parameters, events, schema)."""
    client, headers = _get_client_and_headers(context)
    namespace = params.get("namespace", "")
    name = params.get("name", "")

    if not namespace or not name:
        return ToolResult(
            success=False,
            error="Both namespace and name are required.",
        )

    result = await client.get(
        "/api/core/function/repositoryFind",
        headers=headers,
        params={"namespace": namespace, "name": name},
    )
    if not result.success:
        return ToolResult(
            success=False,
            error=f"Failed to find function {namespace}.{name}: {result.error}",
        )

    fn_def = result.data
    if not fn_def:
        return ToolResult(
            success=False,
            error=f"Function {namespace}.{name} not found in server repository.",
        )

    # Try DSL conversion for LLM-friendly output
    try:
        from app.agents.appbuilder.tools.dsl_bridge import get_dsl_bridge
        bridge = get_dsl_bridge()
        if isinstance(fn_def, dict):
            dsl_text = await bridge.json_to_dsl(fn_def)
            return ToolResult(
                success=True,
                summary=f"Server function {namespace}.{name} (DSL format):\n\n{dsl_text}",
                result_tier=ResultTier.STANDARD,
            )
    except (ImportError, Exception):
        pass

    # Fallback: compact JSON summary
    if isinstance(fn_def, dict):
        compact = {
            "name": fn_def.get("name", name),
            "namespace": fn_def.get("namespace", namespace),
        }
        fn_params = fn_def.get("parameters", {})
        if fn_params:
            compact["parameters"] = {
                k: v.get("schema", {}).get("type", ["?"]) if isinstance(v, dict) else "?"
                for k, v in fn_params.items()
            }
        events = fn_def.get("events", {})
        if events:
            compact["events"] = {
                ename: list(edef.get("parameters", {}).keys()) if isinstance(edef, dict) else []
                for ename, edef in events.items()
            }
        steps = fn_def.get("steps", {})
        compact["step_count"] = len(steps)

        return ToolResult(
            success=True,
            summary=f"Server function {namespace}.{name}:\n{json.dumps(compact, indent=2)}",
            result_tier=ResultTier.STANDARD,
        )

    return ToolResult(
        success=True,
        summary=f"Server function {namespace}.{name}:\n{json.dumps(fn_def, indent=2, default=str)}",
        result_tier=ResultTier.STANDARD,
    )


READ_REMOTE_FUNCTION = ToolDefinition(
    name="read_remote_function",
    description=(
        "Read a server-side (Core) function's signature — parameters, events, and schema. "
        "Use this to understand what params to pass when calling a server function from a page event."
    ),
    parameters=[
        ToolParameter(name="namespace", type="string", description="Function namespace.", required=True),
        ToolParameter(name="name", type="string", description="Function name.", required=True),
    ],
    execute=_execute_read_remote_function,
    is_deferred=True,
    search_hint="read server core function signature parameters events",
    result_tier=ResultTier.STANDARD,
)


# ── Exports ──────────────────────────────────────────────────────

REMOTE_REPO_TOOLS: list[ToolDefinition] = [
    LIST_REMOTE_FUNCTIONS,
    READ_REMOTE_FUNCTION,
]
