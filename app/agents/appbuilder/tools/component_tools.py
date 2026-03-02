"""Component management tools — add, update, read, remove, move.

These are generic tools that work with any component type.
The agent knows valid properties per type from the component catalog
in its system prompt.

All operations use the executor's read-modify-write pattern:
fetch page → modify componentDefinition → save page.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.tools.base import ToolDefinition, ToolResult, ToolParameter
from app.core.tools.http_client import SaasClient
from app.agents.appbuilder.tools._executor import (
    fetch_page_by_name,
    save_page,
    summarize_component,
)


def _get_client_and_headers(context: dict[str, Any]) -> tuple[SaasClient, dict[str, str]]:
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context["headers"]


# ── add_component ───────────────────────────────────────────────

async def _add_component_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    page_name = params["page_name"]
    app_code = params.get("app_code", context.get("app_code", ""))
    parent_key = params["parent_key"]
    component_key = params["component_key"]
    component_type = params["type"]
    properties = params.get("properties", {})
    style_properties = params.get("style_properties", {})
    display_order = params.get("display_order", 0)

    binding_paths = params.get("binding_paths", {})

    page_data, error = await fetch_page_by_name(client, page_name, app_code, headers)
    if error:
        return ToolResult(success=False, error=error)

    comp_def = page_data.setdefault("componentDefinition", {})

    # Check parent exists
    if parent_key not in comp_def:
        return ToolResult(
            success=False,
            error=f"Parent component '{parent_key}' not found. Available: {list(comp_def.keys())}",
        )

    # Check key doesn't already exist
    if component_key in comp_def:
        return ToolResult(
            success=False,
            error=f"Component '{component_key}' already exists. Use update_component to modify it.",
        )

    # Create the component
    new_comp: dict[str, Any] = {
        "key": component_key,
        "type": component_type,
        "name": component_key,
        "displayOrder": display_order,
        "children": {},
        "properties": properties,
        "styleProperties": style_properties if style_properties else {},
    }

    # Set binding paths at top level (not inside properties)
    for bp_key, bp_value in binding_paths.items():
        new_comp[bp_key] = bp_value

    comp_def[component_key] = new_comp

    # Add to parent's children
    parent = comp_def[parent_key]
    parent.setdefault("children", {})[component_key] = True

    # Save
    page_data["message"] = params["message"]
    save_result = await save_page(client, page_data["id"], page_data, headers, context.get("client_code", ""))
    if not save_result.success:
        return save_result

    return ToolResult(
        success=True,
        summary=(
            f"Added {component_type} '{component_key}' under '{parent_key}' "
            f"on page '{page_name}'."
        ),
    )


add_component = ToolDefinition(
    name="add_component",
    display_name="Add Component",
    description=(
        "Add a single new component to a page. "
        "If you need to add or modify MULTIPLE components in one task, use batch_update_page "
        "instead — it fetches and saves the page only once. "
        "Use the component catalog in the system prompt to know valid types and properties. "
        "IMPORTANT: Many components require binding_paths for two-way data binding. "
        "Binding paths live at the TOP LEVEL of the component definition (not inside properties). "
        "Format: {\"bindingPath\": {\"type\": \"VALUE\", \"value\": \"Page.someStore.field\"}}. "
        "Components requiring binding paths:\n"
        "- Popup: bindingPath = boolean toggle (Page.isPopupOpen), controls open/close state\n"
        "- TextBox/TextArea: bindingPath = string value path (Page.form.fieldName)\n"
        "- Dropdown: bindingPath = selected value path\n"
        "- CheckBox: bindingPath = boolean path\n"
        "- ToggleButton: bindingPath = boolean path\n"
        "- ArrayRepeater: bindingPath = array data path (Store.items)\n"
        "- Table: bindingPath = array data path\n"
        "- PhoneNumber: bindingPath = number, bindingPath2 = country code, bindingPath3 = dial code\n"
        "- Gallery/Carousel: bindingPath = toggle visibility\n"
        "- Stepper: bindingPath = current step value\n"
        "- Tabs: bindingPath = active tab value\n"
        "Use VALUE type for direct store paths, EXPRESSION type for computed paths."
    ),
    parameters=[
        ToolParameter(name="page_name", type="string", description="Name of the page."),
        ToolParameter(name="parent_key", type="string", description="Key of the parent component (e.g. 'root')."),
        ToolParameter(name="component_key", type="string", description="Unique key for the new component (e.g. 'emailField', 'submitBtn')."),
        ToolParameter(name="type", type="string", description="Component type (e.g. 'Grid', 'Button', 'TextBox', 'Popup', 'ArrayRepeater')."),
        ToolParameter(name="properties", type="object", description="Component properties as key-value pairs.", required=False),
        ToolParameter(name="style_properties", type="object", description="Style properties in responsive format.", required=False),
        ToolParameter(
            name="binding_paths", type="object", required=False,
            description=(
                "Binding paths for two-way data binding. Keys: bindingPath, bindingPath2 ... bindingPath10. "
                "Each value is a DataLocation: {\"type\": \"VALUE\", \"value\": \"Page.store.path\"} "
                "or {\"type\": \"EXPRESSION\", \"expression\": \"some.expression\"}. "
                "Example: {\"bindingPath\": {\"type\": \"VALUE\", \"value\": \"Page.isModalOpen\"}}"
            ),
        ),
        ToolParameter(name="display_order", type="integer", description="Display order among siblings (default 0).", required=False),
        ToolParameter(name="message", type="string", description="Commit message (10–15 words) describing what was changed."),
        ToolParameter(name="app_code", type="string", description="Application code.", required=False),
    ],
    execute=_add_component_execute,
)


# ── update_component ────────────────────────────────────────────

async def _update_component_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    page_name = params["page_name"]
    app_code = params.get("app_code", context.get("app_code", ""))
    component_key = params["component_key"]
    properties = params.get("properties")
    style_properties = params.get("style_properties")
    display_order = params.get("display_order")

    page_data, error = await fetch_page_by_name(client, page_name, app_code, headers)
    if error:
        return ToolResult(success=False, error=error)

    comp_def = page_data.get("componentDefinition", {})
    if component_key not in comp_def:
        return ToolResult(
            success=False,
            error=f"Component '{component_key}' not found. Available: {list(comp_def.keys())}",
        )

    comp = comp_def[component_key]
    updated_fields = []

    # Merge properties (partial update)
    if properties:
        comp.setdefault("properties", {}).update(properties)
        updated_fields.append(f"properties: {list(properties.keys())}")

    # Merge style properties (partial update)
    if style_properties:
        existing_styles = comp.setdefault("styleProperties", {})
        _deep_merge(existing_styles, style_properties)
        updated_fields.append("styleProperties")

    # Update display order
    if display_order is not None:
        comp["displayOrder"] = display_order
        updated_fields.append(f"displayOrder={display_order}")

    # Set binding paths at top level (not inside properties)
    binding_paths = params.get("binding_paths")
    if binding_paths:
        for bp_key, bp_value in binding_paths.items():
            comp[bp_key] = bp_value
        updated_fields.append(f"bindingPaths: {list(binding_paths.keys())}")

    if not updated_fields:
        return ToolResult(success=True, summary=f"No changes to '{component_key}'.")

    page_data["message"] = params["message"]
    save_result = await save_page(client, page_data["id"], page_data, headers, context.get("client_code", ""))
    if not save_result.success:
        return save_result

    return ToolResult(
        success=True,
        summary=f"Updated '{component_key}' on page '{page_name}': {', '.join(updated_fields)}.",
    )


update_component = ToolDefinition(
    name="update_component",
    display_name="Update Component",
    description=(
        "Update a single existing component's properties, styles, binding paths, or display order. "
        "Properties and styles are merged (partial update). "
        "Use binding_paths to set/update data binding (e.g. the store path a TextBox writes to). "
        "If you need to update MULTIPLE components, use batch_update_page instead."
    ),
    parameters=[
        ToolParameter(name="page_name", type="string", description="Name of the page."),
        ToolParameter(name="component_key", type="string", description="Key of the component to update."),
        ToolParameter(name="properties", type="object", description="Properties to merge (partial update).", required=False),
        ToolParameter(name="style_properties", type="object", description="Style properties to merge.", required=False),
        ToolParameter(
            name="binding_paths", type="object", required=False,
            description=(
                "Binding paths to set at the component's top level. "
                "Keys: bindingPath, bindingPath2, ... bindingPath10. "
                "Each value: {\"type\": \"VALUE\", \"value\": \"Page.store.path\"} "
                "or {\"type\": \"EXPRESSION\", \"expression\": \"expr\"}."
            ),
        ),
        ToolParameter(name="display_order", type="integer", description="New display order.", required=False),
        ToolParameter(name="message", type="string", description="Commit message (10–15 words) describing what was changed."),
        ToolParameter(name="app_code", type="string", description="Application code.", required=False),
    ],
    execute=_update_component_execute,
)


# ── read_component ──────────────────────────────────────────────

async def _read_component_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    page_name = params["page_name"]
    app_code = params.get("app_code", context.get("app_code", ""))
    component_key = params["component_key"]

    page_data, error = await fetch_page_by_name(client, page_name, app_code, headers)
    if error:
        return ToolResult(success=False, error=error)

    comp_def = page_data.get("componentDefinition", {})
    if component_key not in comp_def:
        return ToolResult(
            success=False,
            error=f"Component '{component_key}' not found. Available: {list(comp_def.keys())}",
        )

    comp = comp_def[component_key]
    summary_data = summarize_component(comp, component_key)

    return ToolResult(
        success=True,
        data=comp,
        summary=f"Component '{component_key}' ({comp.get('type', '?')}):\n{json.dumps(summary_data, indent=2, default=str)}",
    )


read_component = ToolDefinition(
    name="read_component",
    display_name="Read Component",
    description="Read a single component's full definition including properties, styles, and children.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Name of the page."),
        ToolParameter(name="component_key", type="string", description="Key of the component to read."),
        ToolParameter(name="app_code", type="string", description="Application code.", required=False),
    ],
    execute=_read_component_execute,
)


# ── remove_component ────────────────────────────────────────────

async def _remove_component_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    page_name = params["page_name"]
    app_code = params.get("app_code", context.get("app_code", ""))
    component_key = params["component_key"]
    recursive = params.get("recursive", True)

    page_data, error = await fetch_page_by_name(client, page_name, app_code, headers)
    if error:
        return ToolResult(success=False, error=error)

    comp_def = page_data.get("componentDefinition", {})
    if component_key not in comp_def:
        return ToolResult(
            success=False,
            error=f"Component '{component_key}' not found.",
        )

    # Don't allow removing root
    if component_key == page_data.get("rootComponent"):
        return ToolResult(success=False, error="Cannot remove the root component.")

    # Collect keys to remove
    keys_to_remove = set()
    if recursive:
        _collect_descendants(comp_def, component_key, keys_to_remove)
    keys_to_remove.add(component_key)

    # Remove from componentDefinition
    for key in keys_to_remove:
        comp_def.pop(key, None)

    # Remove from parent's children
    for comp in comp_def.values():
        children = comp.get("children", {})
        if component_key in children:
            del children[component_key]

    page_data["message"] = params["message"]
    save_result = await save_page(client, page_data["id"], page_data, headers, context.get("client_code", ""))
    if not save_result.success:
        return save_result

    removed = list(keys_to_remove)
    return ToolResult(
        success=True,
        summary=f"Removed {len(removed)} component(s) from page '{page_name}': {removed}",
    )


remove_component = ToolDefinition(
    name="remove_component",
    display_name="Remove Component",
    description="Remove a component from a page. By default also removes all descendants (recursive=true).",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Name of the page."),
        ToolParameter(name="component_key", type="string", description="Key of the component to remove."),
        ToolParameter(name="recursive", type="boolean", description="Also remove all children (default true).", required=False),
        ToolParameter(name="message", type="string", description="Commit message (10–15 words) describing what was changed."),
        ToolParameter(name="app_code", type="string", description="Application code.", required=False),
    ],
    execute=_remove_component_execute,
)


# ── move_component ──────────────────────────────────────────────

async def _move_component_execute(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    client, headers = _get_client_and_headers(context)
    page_name = params["page_name"]
    app_code = params.get("app_code", context.get("app_code", ""))
    component_key = params["component_key"]
    new_parent_key = params["new_parent_key"]
    display_order = params.get("display_order")

    page_data, error = await fetch_page_by_name(client, page_name, app_code, headers)
    if error:
        return ToolResult(success=False, error=error)

    comp_def = page_data.get("componentDefinition", {})

    if component_key not in comp_def:
        return ToolResult(success=False, error=f"Component '{component_key}' not found.")
    if new_parent_key not in comp_def:
        return ToolResult(success=False, error=f"New parent '{new_parent_key}' not found.")

    # Remove from current parent
    for comp in comp_def.values():
        children = comp.get("children", {})
        if component_key in children:
            del children[component_key]

    # Add to new parent
    comp_def[new_parent_key].setdefault("children", {})[component_key] = True

    # Update display order if specified
    if display_order is not None:
        comp_def[component_key]["displayOrder"] = display_order

    page_data["message"] = params["message"]
    save_result = await save_page(client, page_data["id"], page_data, headers, context.get("client_code", ""))
    if not save_result.success:
        return save_result

    return ToolResult(
        success=True,
        summary=f"Moved '{component_key}' to parent '{new_parent_key}' on page '{page_name}'.",
    )


move_component = ToolDefinition(
    name="move_component",
    display_name="Move Component",
    description="Move a component to a different parent or change its display order.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Name of the page."),
        ToolParameter(name="component_key", type="string", description="Key of the component to move."),
        ToolParameter(name="new_parent_key", type="string", description="Key of the new parent component."),
        ToolParameter(name="display_order", type="integer", description="New display order in the new parent.", required=False),
        ToolParameter(name="message", type="string", description="Commit message (10–15 words) describing what was changed."),
        ToolParameter(name="app_code", type="string", description="Application code.", required=False),
    ],
    execute=_move_component_execute,
)


# ── Helpers ─────────────────────────────────────────────────────

def _collect_descendants(
    comp_def: dict[str, Any],
    key: str,
    result: set[str],
) -> None:
    """Recursively collect all descendant keys."""
    comp = comp_def.get(key, {})
    children = comp.get("children", {})
    for child_key, active in children.items():
        if active and child_key in comp_def:
            result.add(child_key)
            _collect_descendants(comp_def, child_key, result)


def _deep_merge(target: dict, source: dict) -> None:
    """Deep merge source into target (modifies target in-place)."""
    for key, value in source.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


# ── Export all component tools ──────────────────────────────────

COMPONENT_TOOLS: list[ToolDefinition] = [
    add_component,
    update_component,
    read_component,
    remove_component,
    move_component,
]
