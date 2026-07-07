"""delete - generic delete tool definition."""

from __future__ import annotations

from typing import Any

from app.core.tools.base import ToolDefinition, ToolResult, ToolParameter
from app.agents.appbuilder.tools.crud._registry import OBJECT_TYPES, OBJECT_TYPE_ENUM
from app.agents.appbuilder.tools.crud._handlers import generic_delete


async def _delete_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    object_type = params["object_type"]
    config = OBJECT_TYPES.get(object_type)
    if not config:
        return ToolResult(success=False, error=f"Unknown object_type: {object_type}")
    return await generic_delete(config, params, context)


delete_tool = ToolDefinition(
    name="delete",
    display_name="Delete",
    # v8 Plan B WS4 · declarative only · blocking elicitation (request_confirmation).
    kind="elicitation",
    elicit_mode="blocking",
    description=(
        "Delete an object. For inherited objects (owned by another client), removes your override.\n\n"
        "For 'application': pass app_code (the application code, not the ID).\n"
        "For all other types: pass id (the MongoDB ObjectId from list)."
    ),
    parameters=[
        ToolParameter(
            name="object_type", type="string",
            description="Type of object to delete.",
            enum=OBJECT_TYPE_ENUM,
        ),
        ToolParameter(name="id", type="string", required=False, description="Object ID to delete. Required for all types except 'application'."),
        ToolParameter(name="app_code", type="string", required=False, description="For object_type='application', the app code to delete."),
    ],
    execute=_delete_execute,
)
