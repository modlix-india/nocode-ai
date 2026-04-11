"""read — generic read tool definition."""

from __future__ import annotations

from typing import Any

from app.core.tools.base import ToolDefinition, ToolResult, ToolParameter
from app.agents.appbuilder.tools.crud._registry import OBJECT_TYPES, OBJECT_TYPE_ENUM
from app.agents.appbuilder.tools.crud._handlers import generic_read


async def _read_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    object_type = params.get("object_type")
    if not object_type:
        return ToolResult(success=False, error="object_type is required. Provide one of: " + ", ".join(OBJECT_TYPE_ENUM))
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
        "- include='summary': condensed page overview — component type counts, top-level sections with descendant counts, event names, bindings, labeled components\n"
        "- include='search': find components by type/name/text/bindings/events using search_* params\n"
        "- include='subtree': detailed subtree with inline properties, event refs, and binding indicators (requires subtree_root)\n"
        "- include='properties': page-level properties (title, permissions, translations, version)\n"
        "- include='events': lists all event functions with step counts\n"
        "- component_key='btnSubmit': reads a specific component's full definition\n"
        "- event_function_name='handleClick': reads a specific event function's KIRun definition\n\n"
        "Recommended workflow for large pages:\n"
        "1. include='summary' to understand the page layout\n"
        "2. include='search' to find specific components\n"
        "3. include='subtree' to explore a section in detail\n"
        "4. component_key to read a specific component's full definition\n\n"
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
        ToolParameter(
            name="include", type="string", required=False,
            enum=["structure", "summary", "search", "subtree", "properties", "events"],
            description=(
                "Page read mode. 'structure' (default): component tree. "
                "'summary': condensed page overview index. "
                "'search': find components (use with search_* params). "
                "'subtree': detailed section view (use with subtree_root). "
                "'properties': page-level props. 'events': list event functions."
            ),
        ),
        ToolParameter(name="subtree_root", type="string", required=False, description="Component key to use as subtree root. Required when include='subtree'."),
        ToolParameter(name="search_type", type="string", required=False, description="Search filter: component type, exact match (e.g. 'Button', 'TextBox', 'Grid'). Only for include='search'."),
        ToolParameter(name="search_name", type="string", required=False, description="Search filter: substring match on component key or name (e.g. 'login', 'header'). Only for include='search'."),
        ToolParameter(name="search_text", type="string", required=False, description="Search filter: substring match on text/label/placeholder properties (e.g. 'Submit', 'Email'). Only for include='search'."),
        ToolParameter(name="search_has_binding", type="boolean", required=False, description="Search filter: only components with data bindings. Only for include='search'."),
        ToolParameter(name="search_has_events", type="boolean", required=False, description="Search filter: only components referenced in event functions. Only for include='search'."),
    ],
    execute=_read_execute,
)
