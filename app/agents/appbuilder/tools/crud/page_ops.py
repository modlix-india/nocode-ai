"""Page sub-operations — list, create, read (structure/properties/events/component/event),
update (properties/component batch/event functions).

Absorbs logic from page_tools.py, component_tools.py, batch_tools.py, event_tools.py.
Reuses _executor.py for fetch/save/tree utilities.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.tools.base import ToolResult
from app.core.tools.http_client import SaasClient
from app.agents.appbuilder.tools._executor import (
    fetch_page_by_name,
    save_page,
    build_component_tree,
    summarize_component,
    build_page_summary,
    search_components,
    build_subtree,
)


def _get_client_and_headers(context: dict[str, Any]) -> tuple[SaasClient, dict[str, str]]:
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context["headers"]


def _resolve_app_code(params: dict[str, Any], context: dict[str, Any]) -> tuple[str, ToolResult | None]:
    from app.agents.appbuilder.tools._shared import resolve_app_code
    app_code = resolve_app_code(params, context)
    if not app_code:
        return "", ToolResult(
            success=False,
            error="No appCode set. Use list(object_type='application') first to find the appCode.",
        )
    return app_code, None


API_PREFIX = "/api/ui/pages"


# ── LIST ──────────────────────────────────────────────────────────


async def page_list(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """List all pages in an application with component counts."""
    client, headers = _get_client_and_headers(context)
    app_code, err = _resolve_app_code(params, context)
    if err:
        return err

    result = await client.get(
        API_PREFIX,
        headers=headers,
        params={"page": 0, "size": 1000, "appCode": app_code},
    )
    if not result.success:
        return ToolResult(success=False, error=f"Failed to list pages: {result.error}")

    data = result.data
    pages = data.get("content", []) if isinstance(data, dict) else []

    lines = []
    for page in pages:
        name = page.get("name", "?")
        page_id = page.get("id", "?")
        version = page.get("version", "?")
        lines.append(f"- {name} (id={page_id}, v{version})")

    return ToolResult(
        success=True,
        data={"pages": [{"name": p.get("name"), "id": p.get("id"), "version": p.get("version")} for p in pages]},
        summary=f"Found {len(pages)} pages:\n" + "\n".join(lines),
    )


# ── CREATE ────────────────────────────────────────────────────────


async def page_create(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Create a new page with a root Grid component."""
    from app.agents.appbuilder.tools._shared import validate_name

    client, headers = _get_client_and_headers(context)
    app_code, err = _resolve_app_code(params, context)
    if err:
        return err
    page_name = params["name"]

    err = validate_name(page_name)
    if err:
        return err

    title = params.get("title", page_name)
    root_key = "root"
    page_data: dict[str, Any] = {
        "name": page_name,
        "appCode": app_code,
        "clientCode": context.get("client_code", ""),
        "title": title,
        "rootComponent": root_key,
        "componentDefinition": {
            root_key: {
                "key": root_key,
                "type": "Grid",
                "name": root_key,
                "displayOrder": 0,
                "children": {},
                "properties": {},
                "styleProperties": {},
            }
        },
        "eventFunctions": {},
        "properties": {},
        "translations": {},
    }
    if params.get("description"):
        page_data["description"] = params["description"]
    page_data["message"] = params["message"]

    result = await client.post(API_PREFIX, headers=headers, json=page_data)
    if not result.success:
        return ToolResult(success=False, error=f"Failed to create page: {result.error}")

    created = result.data
    page_id = created.get("id", "?") if isinstance(created, dict) else "?"
    return ToolResult(
        success=True,
        data={"id": page_id, "name": page_name},
        summary=f"Created page '{page_name}' (id={page_id}) with root Grid component.",
    )


# ── READ ──────────────────────────────────────────────────────────


async def page_read(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Read page data — dispatches based on params.

    - component_key → specific component
    - event_function_name → specific event function
    - include=properties → page-level properties
    - include=events → list event functions
    - default → component tree structure
    """
    client, headers = _get_client_and_headers(context)
    page_name = params.get("name", "")
    app_code = _resolve_app_code(params, context)[0]

    if not page_name:
        return ToolResult(success=False, error="'name' (page name) is required to read a page.")
    if not app_code:
        return ToolResult(
            success=False,
            error="No appCode set. Use list(object_type='application') first to find the appCode.",
        )

    page_data, error = await fetch_page_by_name(client, page_name, app_code, headers)
    if error:
        return ToolResult(success=False, error=error)

    # Sub-operation: read specific component
    component_key = params.get("component_key")
    if component_key:
        return _read_component(page_data, page_name, component_key)

    # Sub-operation: read specific event function
    event_fn_name = params.get("event_function_name")
    if event_fn_name:
        return _read_event_function(page_data, page_name, event_fn_name)

    # Include mode
    include = params.get("include", "structure")

    if include == "properties":
        return _read_page_properties(page_data, page_name)

    if include == "events":
        return _read_event_functions_list(page_data, page_name)

    if include == "summary":
        return ToolResult(success=True, summary=build_page_summary(page_data))

    if include == "search":
        filters = {
            "search_type": params.get("search_type"),
            "search_name": params.get("search_name"),
            "search_text": params.get("search_text"),
            "search_has_binding": params.get("search_has_binding", False),
            "search_has_events": params.get("search_has_events", False),
        }
        results = search_components(page_data, filters)
        if not results:
            return ToolResult(success=True, summary=f"No components matched the search filters on page '{page_name}'.")
        return ToolResult(
            success=True,
            data=results,
            summary=f"Found {len(results)} matching component(s) on page '{page_name}':\n{json.dumps(results, indent=2, default=str)}",
        )

    if include == "subtree":
        subtree_root = params.get("subtree_root")
        if not subtree_root:
            return ToolResult(success=False, error="'subtree_root' is required when include='subtree'.")
        return ToolResult(success=True, summary=build_subtree(page_data, subtree_root))

    # Default: component tree structure
    tree = build_component_tree(page_data)
    comp_count = len(page_data.get("componentDefinition", {}))
    event_count = len(page_data.get("eventFunctions", {}))
    summary = (
        f"Page '{page_name}' structure ({comp_count} components, {event_count} event functions):\n\n"
        f"{tree}"
    )
    return ToolResult(success=True, summary=summary)


def _read_component(page_data: dict, page_name: str, component_key: str) -> ToolResult:
    """Read a specific component's full definition."""
    comp_def = page_data.get("componentDefinition", {})
    if component_key not in comp_def:
        return ToolResult(
            success=False,
            error=f"Component '{component_key}' not found. Available: {list(comp_def.keys())}",
        )
    comp = comp_def[component_key]
    # Return compact summary: properties and children, skip bulky styleProperties
    compact = {
        "key": component_key,
        "type": comp.get("type", "?"),
        "properties": comp.get("properties", {}),
        "children": list(comp.get("children", {}).keys()),
        "displayOrder": comp.get("displayOrder"),
    }
    # Include binding paths if present
    for k in ("bindingPath", "bindingPath2"):
        if k in comp:
            compact[k] = comp[k]
    # Include style property keys (not full values) for awareness
    style_props = comp.get("styleProperties", {})
    if style_props:
        compact["stylePropertyKeys"] = list(style_props.keys())
    return ToolResult(
        success=True,
        data=compact,
        summary=f"Component '{component_key}' ({comp.get('type', '?')}):\n{json.dumps(compact, indent=2, default=str)}",
    )


def _read_event_function(page_data: dict, page_name: str, function_name: str) -> ToolResult:
    """Read a specific event function definition."""
    event_functions = page_data.get("eventFunctions", {})
    if function_name not in event_functions:
        return ToolResult(
            success=False,
            error=f"Event function '{function_name}' not found. Available: {list(event_functions.keys())}",
        )
    definition = event_functions[function_name]
    # Return compact summary: step names and namespaces, not full parameterMaps
    steps = definition.get("steps", {})
    compact_steps = {}
    for step_name, step_def in steps.items():
        compact_steps[step_name] = {
            "namespace": step_def.get("namespace", "?"),
            "name": step_def.get("name", "?"),
            "dependentSteps": step_def.get("dependentSteps", []),
        }
        # Include parameterMap keys (not values) for awareness
        param_map = step_def.get("parameterMap", {})
        if param_map:
            compact_steps[step_name]["parameterMapKeys"] = list(param_map.keys())
    compact = {
        "name": definition.get("name", function_name),
        "namespace": definition.get("namespace", ""),
        "steps": compact_steps,
    }
    return ToolResult(
        success=True,
        data=definition,
        summary=f"Event function '{function_name}' ({len(steps)} steps):\n{json.dumps(compact, indent=2, default=str)}",
    )


def _read_page_properties(page_data: dict, page_name: str) -> ToolResult:
    """Read page-level properties (title, description, translations, permissions)."""
    page_props = page_data.get("properties", {})
    props = {
        "id": page_data.get("id"),
        "name": page_data.get("name"),
        "description": page_data.get("description"),
        "rootComponent": page_data.get("rootComponent"),
        "properties": page_props,
        "translations": page_data.get("translations", {}),
        "permission": page_data.get("permission"),
        "version": page_data.get("version"),
    }
    title_obj = page_props.get("title", {})
    title_name = title_obj.get("name", {})
    if isinstance(title_name, dict):
        props["_pageTitle"] = title_name.get("value") or title_name.get("location", {}).get("expression", "")

    return ToolResult(
        success=True,
        data=props,
        summary=f"Page '{page_name}' properties:\n{json.dumps(props, indent=2, default=str)}",
    )


def _read_event_functions_list(page_data: dict, page_name: str) -> ToolResult:
    """List all event functions with step counts."""
    event_functions = page_data.get("eventFunctions", {})
    if not event_functions:
        return ToolResult(success=True, summary=f"Page '{page_name}' has no event functions.")

    lines = []
    for name, defn in event_functions.items():
        steps = defn.get("steps", {})
        step_count = len(steps) if isinstance(steps, dict) else 0
        lines.append(f"- {name} ({step_count} steps)")

    return ToolResult(success=True, summary=f"Event functions on page '{page_name}':\n" + "\n".join(lines))


# ── UPDATE ────────────────────────────────────────────────────────


async def page_update(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Update page — supports properties, component batch ops, event functions.

    Can combine multiple sub-operations in a single call (one fetch + one save).
    """
    client, headers = _get_client_and_headers(context)
    page_name = params.get("page_name", "")
    app_code = _resolve_app_code(params, context)[0]

    if not page_name:
        return ToolResult(success=False, error="'page_name' is required for page update.")
    if not app_code:
        return ToolResult(
            success=False,
            error="No appCode set. Use list(object_type='application') first.",
        )

    page_data, error = await fetch_page_by_name(client, page_name, app_code, headers)
    if error:
        return ToolResult(success=False, error=error)

    changes = []

    # Sub-operation: update page properties
    properties = params.get("properties")
    if properties:
        _apply_page_properties(page_data, properties)
        changes.append(f"properties: {list(properties.keys())}")

    # Sub-operation: batch component operations
    operations = params.get("operations")
    if operations:
        op_result = _apply_component_operations(page_data, operations)
        changes.append(op_result)

    # Sub-operation: write/update event function
    event_function = params.get("event_function")
    if event_function:
        fn_name = event_function.get("function_name", "")
        fn_def = event_function.get("definition", {})
        if fn_name and fn_def:
            page_data.setdefault("eventFunctions", {})[fn_name] = fn_def
            steps = fn_def.get("steps", {})
            step_count = len(steps) if isinstance(steps, dict) else 0
            changes.append(f"wrote event function '{fn_name}' ({step_count} steps)")

    # Sub-operation: delete event function
    delete_event = params.get("delete_event_function")
    if delete_event:
        event_functions = page_data.get("eventFunctions", {})
        if delete_event in event_functions:
            del event_functions[delete_event]
            changes.append(f"deleted event function '{delete_event}'")
        else:
            changes.append(f"event function '{delete_event}' not found (skipped)")

    if not changes:
        return ToolResult(success=False, error="No update operations specified.")

    page_data["message"] = params["message"]
    save_result = await save_page(client, page_data["id"], page_data, headers, context.get("client_code", ""))
    if not save_result.success:
        return save_result

    return ToolResult(
        success=True,
        summary=f"Updated page '{page_name}': {'; '.join(changes)}.",
    )


def _apply_page_properties(page_data: dict, updates: dict) -> None:
    """Merge page-level property updates into page data."""
    for key, value in updates.items():
        if key == "title":
            page_props = page_data.setdefault("properties", {})
            title_obj = page_props.setdefault("title", {})
            if isinstance(value, str):
                title_obj["name"] = {"value": value}
            elif isinstance(value, dict):
                title_obj.update(value)
        elif key in ("permission", "description"):
            page_data[key] = value
        elif key == "translations":
            page_data.setdefault("translations", {}).update(value)
        elif key == "properties":
            page_data.setdefault("properties", {}).update(value)


def _apply_component_operations(page_data: dict, operations: list[dict]) -> str:
    """Apply batch component operations. Returns summary string."""
    comp_def = page_data.setdefault("componentDefinition", {})
    root_key = page_data.get("rootComponent", "")

    errors = []
    applied = []

    for i, op in enumerate(operations):
        op_type = op.get("op")
        err = None

        if op_type == "add":
            err = _op_add(comp_def, op)
        elif op_type == "update":
            err = _op_update(comp_def, op)
        elif op_type == "remove":
            err = _op_remove(comp_def, op, root_key)
        elif op_type == "move":
            err = _op_move(comp_def, op)
        else:
            err = f"Unknown op type '{op_type}' (must be add/update/remove/move)"

        if err:
            errors.append(f"op[{i}] ({op_type}): {err}")
        else:
            key = op.get("component_key", op.get("parent_key", "?"))
            applied.append(f"{op_type} '{key}'")

    parts = [f"applied {len(applied)} op(s): {', '.join(applied)}"]
    if errors:
        parts.append(f"skipped {len(errors)} error(s): {'; '.join(errors)}")
    return "; ".join(parts)


# ── Component operation handlers ──────────────────────────────────


def _op_add(comp_def: dict, op: dict) -> str | None:
    """Add a new component. Returns error string or None."""
    parent_key = op.get("parent_key")
    component_key = op.get("component_key")
    component_type = op.get("type")

    if not parent_key or not component_key or not component_type:
        return "add op missing parent_key/component_key/type"
    if parent_key not in comp_def:
        return f"Parent '{parent_key}' not found"
    if component_key in comp_def:
        return f"Component '{component_key}' already exists; use update instead"

    new_comp: dict = {
        "key": component_key,
        "type": component_type,
        "name": component_key,
        "displayOrder": op.get("display_order", 0),
        "children": {},
        "properties": op.get("properties", {}),
        "styleProperties": op.get("style_properties", {}),
    }
    for bp_key, bp_value in op.get("binding_paths", {}).items():
        new_comp[bp_key] = bp_value

    comp_def[component_key] = new_comp
    comp_def[parent_key].setdefault("children", {})[component_key] = True
    return None


def _op_update(comp_def: dict, op: dict) -> str | None:
    """Merge properties/styles into a component. Returns error string or None."""
    component_key = op.get("component_key")
    if not component_key:
        return "update op missing component_key"
    if component_key not in comp_def:
        return f"Component '{component_key}' not found"

    comp = comp_def[component_key]
    if op.get("properties"):
        comp.setdefault("properties", {}).update(op["properties"])
    if op.get("style_properties"):
        _deep_merge(comp.setdefault("styleProperties", {}), op["style_properties"])
    if op.get("display_order") is not None:
        comp["displayOrder"] = op["display_order"]
    for bp_key, bp_value in op.get("binding_paths", {}).items():
        comp[bp_key] = bp_value
    return None


def _op_remove(comp_def: dict, op: dict, root_key: str) -> str | None:
    """Remove a component (and descendants). Returns error string or None."""
    component_key = op.get("component_key")
    if not component_key:
        return "remove op missing component_key"
    if component_key not in comp_def:
        return f"Component '{component_key}' not found"
    if component_key == root_key:
        return "Cannot remove the root component"

    keys_to_remove: set[str] = set()
    if op.get("recursive", True):
        _collect_descendants(comp_def, component_key, keys_to_remove)
    keys_to_remove.add(component_key)

    for key in keys_to_remove:
        comp_def.pop(key, None)
    for comp in comp_def.values():
        comp.get("children", {}).pop(component_key, None)
    return None


def _op_move(comp_def: dict, op: dict) -> str | None:
    """Move a component to a new parent. Returns error string or None."""
    component_key = op.get("component_key")
    new_parent_key = op.get("new_parent_key")
    if not component_key or not new_parent_key:
        return "move op missing component_key or new_parent_key"
    if component_key not in comp_def:
        return f"Component '{component_key}' not found"
    if new_parent_key not in comp_def:
        return f"New parent '{new_parent_key}' not found"

    for comp in comp_def.values():
        comp.get("children", {}).pop(component_key, None)
    comp_def[new_parent_key].setdefault("children", {})[component_key] = True

    if op.get("display_order") is not None:
        comp_def[component_key]["displayOrder"] = op["display_order"]
    return None


# ── Helpers ───────────────────────────────────────────────────────


def _collect_descendants(comp_def: dict[str, Any], key: str, result: set[str]) -> None:
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
