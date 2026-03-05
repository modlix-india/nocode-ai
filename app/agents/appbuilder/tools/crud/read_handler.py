"""read — generic read tool definition."""

from __future__ import annotations

from typing import Any

from app.core.tools.base import ToolDefinition, ToolResult, ToolParameter
from app.agents.appbuilder.tools.crud._registry import OBJECT_TYPES, OBJECT_TYPE_ENUM
from app.agents.appbuilder.tools.crud._handlers import generic_read


async def _read_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    object_type = params["object_type"]
    config = OBJECT_TYPES.get(object_type)
    if not config:
        return ToolResult(success=False, error=f"Unknown object_type: {object_type}")
    return await generic_read(config, params, context)


read_tool = ToolDefinition(
    name="read",
    display_name="Read",
    description=(
        "Read an object's full definition or specific sub-parts.\n\n"
        "For most types: pass id (the MongoDB ObjectId from list).\n"
        "For pages: pass name (page name) + app_code instead of id.\n\n"
        "Page sub-operations (object_type='page'):\n"
        "- Default: returns component tree structure (hierarchy with types and labels)\n"
        "- include='properties': page-level properties (title, permissions, translations, version)\n"
        "- include='events': lists all event functions with step counts\n"
        "- component_key='btnSubmit': reads a specific component's full definition\n"
        "- event_function_name='handleClick': reads a specific event function's KIRun definition\n\n"
        "For 'application':\n"
        "- id=UI app ID: reads full app definition (named pages, themes, fontPacks, settings)\n"
        "- app_code (no id): lists UI application definitions for that appCode, "
        "returning their MongoDB ObjectIds. Use list(object_type='application') to get security IDs, "
        "then read(object_type='application', app_code='X') for UI IDs, "
        "then read(object_type='application', id='UI_ID') for the full definition."
    ),
    parameters=[
        ToolParameter(
            name="object_type", type="string",
            description="Type of object to read.",
            enum=OBJECT_TYPE_ENUM,
        ),
        ToolParameter(name="id", type="string", required=False, description="Object ID (MongoDB ObjectId). Required for all types except 'page' and 'application' by app_code."),
        ToolParameter(name="name", type="string", required=False, description="Object name. Used instead of 'id' for pages (fetches by name + appCode)."),
        ToolParameter(name="app_code", type="string", required=False, description="Application code. Required when reading pages by name. For 'application', lists UI defs for that appCode."),
        ToolParameter(name="component_key", type="string", required=False, description="Page sub-op: read a specific component's full definition. Only for object_type='page'."),
        ToolParameter(name="event_function_name", type="string", required=False, description="Page sub-op: read a specific event function definition. Only for object_type='page'."),
        ToolParameter(name="include", type="string", required=False, enum=["structure", "properties", "events"], description="Page read mode. 'structure' (default): component tree. 'properties': page-level props. 'events': list event functions."),
    ],
    execute=_read_execute,
)
