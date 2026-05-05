"""list — generic list tool definition."""

from __future__ import annotations

from typing import Any

from app.core.tools.base import ToolDefinition, ToolResult, ToolParameter
from app.agents.appbuilder.tools.crud._registry import OBJECT_TYPES, OBJECT_TYPE_ENUM
from app.agents.appbuilder.tools.crud._handlers import generic_list


async def _list_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    object_type = params.get("object_type")
    if not object_type:
        return ToolResult(success=False, error="object_type is required. Provide one of: " + ", ".join(OBJECT_TYPE_ENUM))
    config = OBJECT_TYPES.get(object_type)
    if not config:
        return ToolResult(success=False, error=f"Unknown object_type: {object_type}")
    return await generic_list(config, params, context)


list_tool = ToolDefinition(
    name="list",
    display_name="List",
    description=(
        "List objects in the application by type. Returns names, IDs, and version numbers.\n\n"
        "Object types:\n"
        "- page: pages with component counts\n"
        "- application: search apps by name/code. ALWAYS call this first to confirm appCode.\n"
        "- theme/style/function/schema: UI entity listings\n"
        "- connection/workflow/template/uripath: core/UI entity listings\n\n"
        "For 'application', app_code acts as a search term (matches both appName and appCode).\n"
        "For all other types, app_code selects which application to list entities from."
    ),
    parameters=[
        ToolParameter(
            name="object_type", type="string",
            description="Type of object to list.",
            enum=OBJECT_TYPE_ENUM,
        ),
        ToolParameter(
            name="app_code", type="string", required=False,
            description="Application code. For 'application' type, used as a search term. For others, selects the app.",
        ),
    ],
    execute=_list_execute,
)
