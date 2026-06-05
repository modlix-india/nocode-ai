"""update — generic update tool definition."""

from __future__ import annotations

from typing import Any

from app.core.tools.base import ToolDefinition, ToolResult, ToolParameter
from app.agents.appbuilder.tools.crud._registry import OBJECT_TYPES, OBJECT_TYPE_ENUM
from app.agents.appbuilder.tools.crud._handlers import generic_update


async def _update_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    object_type = params["object_type"]
    config = OBJECT_TYPES.get(object_type)
    if not config:
        return ToolResult(success=False, error=f"Unknown object_type: {object_type}")
    return await generic_update(config, params, context)


update_tool = ToolDefinition(
    name="update",
    display_name="Update",
    # v8 Plan B WS4 · declarative only · blocking elicitation (request_confirmation).
    kind="elicitation",
    elicit_mode="blocking",
    description=(
        "Update an existing object. Fetches current state, merges changes, saves.\n\n"
        "For most types: pass id + fields to update (definition, name, title, description).\n"
        "For pages: pass page_name + app_code instead of id.\n\n"
        "Page sub-operations (object_type='page', can combine multiple in one call):\n"
        "- properties={...}: update page-level properties. title accepts string or "
        "{name: {value: str}, append: {value: bool}}.\n"
        "- operations=[...]: batch component operations (single fetch+save). Each op:\n"
        "  {op:'add', parent_key, component_key, type, properties?, style_properties?, binding_paths?, display_order?}\n"
        "  {op:'update', component_key, properties?, style_properties?, binding_paths?, display_order?}\n"
        "  {op:'remove', component_key, recursive?}\n"
        "  {op:'move', component_key, new_parent_key, display_order?}\n"
        "  Properties MUST use ComponentProperty format: {\"value\": \"x\"} not bare strings.\n"
        "  style_properties: {\"key\": {\"resolutions\": {\"ALL\": {\"cssProp\": {\"value\": \"val\"}}}}}.\n"
        "  binding_paths: {\"bindingPath\": {\"value\": \"Page.store.field\"}} at component top level.\n"
        "- event_function={function_name: \"handleLogin\", definition: {name, namespace, steps, events}}: "
        "write/update event function\n"
        "- delete_event_function=\"functionName\": remove an event function\n\n"
        "Theme-specific: variables={breakpoint: {key: value}} to merge. MUST set confirmed=true.\n"
        "Application-specific: properties={...} to merge into app definition, id=UI app ID."
    ),
    parameters=[
        ToolParameter(
            name="object_type", type="string",
            description="Type of object to update.",
            enum=OBJECT_TYPE_ENUM,
        ),
        ToolParameter(name="message", type="string", description="Commit message (10-15 words) describing what was changed."),
        ToolParameter(name="id", type="string", required=False, description="Object ID. Required for all types except 'page' (which uses page_name)."),
        ToolParameter(name="page_name", type="string", required=False, description="Page name. Used instead of 'id' for page operations."),
        ToolParameter(name="app_code", type="string", required=False, description="Application code."),
        ToolParameter(name="definition", type="object", required=False, description="New definition. For function/schema/style/connection/workflow/template/uripath."),
        ToolParameter(name="name", type="string", required=False, description="New name for the object."),
        ToolParameter(name="title", type="string", required=False, description="New title."),
        ToolParameter(name="description", type="string", required=False, description="New description."),
        ToolParameter(
            name="properties", type="object", required=False,
            description="For 'page': page-level properties (title, permission, description, translations). For 'application': app properties to merge.",
        ),
        ToolParameter(
            name="operations", type="array", required=False,
            items={"type": "object"},
            description=(
                "Page component batch operations. Each: "
                "{op:'add', parent_key, component_key, type, properties?, style_properties?, binding_paths?, display_order?} | "
                "{op:'update', component_key, properties?, style_properties?, binding_paths?, display_order?} | "
                "{op:'remove', component_key, recursive?} | "
                "{op:'move', component_key, new_parent_key, display_order?}"
            ),
        ),
        ToolParameter(
            name="event_function", type="object", required=False,
            description="Write/update an event function on a page. {function_name: str, definition: {name, namespace, steps, events}}.",
        ),
        ToolParameter(name="delete_event_function", type="string", required=False, description="Delete an event function by name from a page."),
        ToolParameter(name="variables", type="object", required=False, description="Theme variables to merge by breakpoint. Only for object_type='theme'."),
        ToolParameter(name="confirmed", type="boolean", required=False, description="Required for theme updates. Must be true after user approves."),
    ],
    execute=_update_execute,
)
