"""Event function tools — write, read, list event functions.

Event functions are stored in page.eventFunctions as a map of
function name → KIRun function definition (JSON).

The agent provides event function definitions as JSON objects.
KIRun DSL support may be added later.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.tools.base import ToolDefinition, ToolResult, ToolParameter
from app.core.tools.http_client import SaasClient
from app.agents.appbuilder.tools._executor import (
    fetch_page_by_name,
    save_page,
)


def _get_client_and_headers(context: dict[str, Any]) -> tuple[SaasClient, dict[str, str]]:
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context["headers"]


# ── write_event_function ────────────────────────────────────────

async def _write_event_function_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    page_name = params["page_name"]
    app_code = params.get("app_code", context.get("app_code", ""))
    function_name = params["function_name"]
    definition = params["definition"]

    page_data, error = await fetch_page_by_name(client, page_name, app_code, headers)
    if error:
        return ToolResult(success=False, error=error)

    # Add/replace the event function
    event_functions = page_data.setdefault("eventFunctions", {})
    event_functions[function_name] = definition

    page_data["message"] = params["message"]
    save_result = await save_page(client, page_data["id"], page_data, headers, context.get("client_code", ""))
    if not save_result.success:
        return save_result

    # Count steps for summary
    steps = definition.get("steps", {})
    step_count = len(steps) if isinstance(steps, dict) else 0

    return ToolResult(
        success=True,
        summary=f"Wrote event function '{function_name}' on page '{page_name}' ({step_count} steps).",
    )


write_event_function = ToolDefinition(
    name="write_event_function",
    display_name="Write Event Function",
    description=(
        "Write an event function to a page. Event functions are triggered by component events "
        "(onClick, onChange, etc.) and define a sequence of steps using KIRun functions. "
        "Provide the full function definition as a JSON object with 'name', 'namespace', "
        "'steps', and 'events' fields."
    ),
    parameters=[
        ToolParameter(name="page_name", type="string", description="Name of the page."),
        ToolParameter(
            name="function_name",
            type="string",
            description="Name of the event function (e.g. 'handleLogin', 'onSearchChange').",
        ),
        ToolParameter(
            name="definition",
            type="object",
            description=(
                "KIRun function definition object with: "
                "name (string), namespace (string), "
                "steps (object mapping step names to step definitions), "
                "events (object mapping event names to event definitions)."
            ),
        ),
        ToolParameter(name="message", type="string", description="Commit message (10–15 words) describing what was changed."),
        ToolParameter(name="app_code", type="string", description="Application code.", required=False),
    ],
    execute=_write_event_function_execute,
)


# ── read_event_function ─────────────────────────────────────────

async def _read_event_function_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    page_name = params["page_name"]
    app_code = params.get("app_code", context.get("app_code", ""))
    function_name = params["function_name"]

    page_data, error = await fetch_page_by_name(client, page_name, app_code, headers)
    if error:
        return ToolResult(success=False, error=error)

    event_functions = page_data.get("eventFunctions", {})
    if function_name not in event_functions:
        available = list(event_functions.keys())
        return ToolResult(
            success=False,
            error=f"Event function '{function_name}' not found. Available: {available}",
        )

    definition = event_functions[function_name]

    return ToolResult(
        success=True,
        data=definition,
        summary=f"Event function '{function_name}':\n{json.dumps(definition, indent=2, default=str)}",
    )


read_event_function = ToolDefinition(
    name="read_event_function",
    display_name="Read Event Function",
    description="Read an event function's full definition from a page.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Name of the page."),
        ToolParameter(name="function_name", type="string", description="Name of the event function to read."),
        ToolParameter(name="app_code", type="string", description="Application code.", required=False),
    ],
    execute=_read_event_function_execute,
)


# ── list_event_functions ────────────────────────────────────────

async def _list_event_functions_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    page_name = params["page_name"]
    app_code = params.get("app_code", context.get("app_code", ""))

    page_data, error = await fetch_page_by_name(client, page_name, app_code, headers)
    if error:
        return ToolResult(success=False, error=error)

    event_functions = page_data.get("eventFunctions", {})

    if not event_functions:
        return ToolResult(success=True, summary=f"Page '{page_name}' has no event functions.")

    lines = []
    for name, defn in event_functions.items():
        steps = defn.get("steps", {})
        step_count = len(steps) if isinstance(steps, dict) else 0
        lines.append(f"- {name} ({step_count} steps)")

    summary = f"Event functions on page '{page_name}':\n" + "\n".join(lines)
    return ToolResult(success=True, summary=summary)


list_event_functions = ToolDefinition(
    name="list_event_functions",
    display_name="List Event Functions",
    description="List all event functions on a page with their step counts.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Name of the page."),
        ToolParameter(name="app_code", type="string", description="Application code.", required=False),
    ],
    execute=_list_event_functions_execute,
)


# ── delete_event_function ───────────────────────────────────────

async def _delete_event_function_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    page_name = params["page_name"]
    app_code = params.get("app_code", context.get("app_code", ""))
    function_name = params["function_name"]

    page_data, error = await fetch_page_by_name(client, page_name, app_code, headers)
    if error:
        return ToolResult(success=False, error=error)

    event_functions = page_data.get("eventFunctions", {})
    if function_name not in event_functions:
        return ToolResult(success=False, error=f"Event function '{function_name}' not found.")

    del event_functions[function_name]

    page_data["message"] = params["message"]
    save_result = await save_page(client, page_data["id"], page_data, headers, context.get("client_code", ""))
    if not save_result.success:
        return save_result

    return ToolResult(
        success=True,
        summary=f"Deleted event function '{function_name}' from page '{page_name}'.",
    )


delete_event_function = ToolDefinition(
    name="delete_event_function",
    display_name="Delete Event Function",
    description="Delete an event function from a page.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Name of the page."),
        ToolParameter(name="function_name", type="string", description="Name of the event function to delete."),
        ToolParameter(name="message", type="string", description="Commit message (10–15 words) describing what was changed."),
        ToolParameter(name="app_code", type="string", description="Application code.", required=False),
    ],
    execute=_delete_event_function_execute,
)


# ── Export all event tools ──────────────────────────────────────

EVENT_TOOLS: list[ToolDefinition] = [
    write_event_function,
    read_event_function,
    list_event_functions,
    delete_event_function,
]
