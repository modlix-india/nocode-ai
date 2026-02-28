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
    comp_def[component_key] = {
        "key": component_key,
        "type": component_type,
        "name": component_key,
        "displayOrder": display_order,
        "children": {},
        "properties": properties,
        "styleProperties": style_properties if style_properties else {},
    }

    # Add to parent's children
    parent = comp_def[parent_key]
    parent.setdefault("children", {})[component_key] = True

    # Save
    save_result = await save_page(client, page_data["id"], page_data, headers)
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
    description=(
        "Add a new component to a page. The component is added to the flat "
        "componentDefinition map and registered as a child of parent_key. "
        "Use the component catalog in the system prompt to know valid types and properties."
    ),
    parameters=[
        ToolParameter(name="page_name", type="string", description="Name of the page."),
        ToolParameter(name="parent_key", type="string", description="Key of the parent component (e.g. 'root')."),
        ToolParameter(name="component_key", type="string", description="Unique key for the new component (e.g. 'emailField', 'submitBtn')."),
        ToolParameter(name="type", type="string", description="Component type (e.g. 'Grid', 'Button', 'TextBox', 'Text', 'Image')."),
        ToolParameter(name="properties", type="object", description="Component properties as key-value pairs.", required=False),
        ToolParameter(name="style_properties", type="object", description="Style properties in responsive format: {screenSize: {pseudoState: {cssProp: value}}}.", required=False),
        ToolParameter(name="display_order", type="integer", description="Display order among siblings (default 0).", required=False),
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

    if not updated_fields:
        return ToolResult(success=True, summary=f"No changes to '{component_key}'.")

    save_result = await save_page(client, page_data["id"], page_data, headers)
    if not save_result.success:
        return save_result

    return ToolResult(
        success=True,
        summary=f"Updated '{component_key}' on page '{page_name}': {', '.join(updated_fields)}.",
    )


update_component = ToolDefinition(
    name="update_component",
    description=(
        "Update an existing component's properties, styles, or display order. "
        "Properties and styles are merged (partial update) — you only need to "
        "specify the fields you want to change."
    ),
    parameters=[
        ToolParameter(name="page_name", type="string", description="Name of the page."),
        ToolParameter(name="component_key", type="string", description="Key of the component to update."),
        ToolParameter(name="properties", type="object", description="Properties to merge (partial update).", required=False),
        ToolParameter(name="style_properties", type="object", description="Style properties to merge.", required=False),
        ToolParameter(name="display_order", type="integer", description="New display order.", required=False),
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

    save_result = await save_page(client, page_data["id"], page_data, headers)
    if not save_result.success:
        return save_result

    removed = list(keys_to_remove)
    return ToolResult(
        success=True,
        summary=f"Removed {len(removed)} component(s) from page '{page_name}': {removed}",
    )


remove_component = ToolDefinition(
    name="remove_component",
    description="Remove a component from a page. By default also removes all descendants (recursive=true).",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Name of the page."),
        ToolParameter(name="component_key", type="string", description="Key of the component to remove."),
        ToolParameter(name="recursive", type="boolean", description="Also remove all children (default true).", required=False),
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

    save_result = await save_page(client, page_data["id"], page_data, headers)
    if not save_result.success:
        return save_result

    return ToolResult(
        success=True,
        summary=f"Moved '{component_key}' to parent '{new_parent_key}' on page '{page_name}'.",
    )


move_component = ToolDefinition(
    name="move_component",
    description="Move a component to a different parent or change its display order.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Name of the page."),
        ToolParameter(name="component_key", type="string", description="Key of the component to move."),
        ToolParameter(name="new_parent_key", type="string", description="Key of the new parent component."),
        ToolParameter(name="display_order", type="integer", description="New display order in the new parent.", required=False),
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
