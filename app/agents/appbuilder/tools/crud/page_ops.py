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
    app_code = params.get("app_code") or context.get("app_code", "")
    if not app_code:
        return "", ToolResult(
            success=False,
            error="No appCode set. Use list(object_type='application') first to find the appCode.",
        )
    return app_code, None


API_PREFIX = "/api/ui/pages"


# ── Session-scoped memory ─────────────────────────────────────────
# Populates session.context["known"] as tools discover entities. The
# dynamic context builder surfaces this to the LLM on each turn so it
# doesn't re-list/re-read things it already knows. Persisted to DB via
# session.context_json, so it survives across turns (including server
# restarts).

_MAX_KNOWN_PAGES_PER_APP = 30
_MAX_KNOWN_COMPONENTS_PER_PAGE = 60


def _get_known(context: dict[str, Any]) -> dict[str, Any]:
    """Get (and initialize if needed) the 'known' memo on the session context."""
    session_ctx = context.get("session_context")
    if session_ctx is None:
        return {}  # no session; writes are a no-op
    return session_ctx.setdefault("known", {
        "pages": {},          # "{app_code}/{page_name}" → {id, component_count, top_sections}
        "applications": {},   # "{app_code}" → {ui_app_id, app_type, page_names}
    })


def _remember_page(
    context: dict[str, Any],
    app_code: str,
    page_name: str,
    page_data: dict[str, Any],
) -> None:
    """Record what we learned about a page so the agent doesn't re-fetch it."""
    known = _get_known(context)
    if not known:
        return
    pages = known.setdefault("pages", {})
    key = f"{app_code}/{page_name}"
    comp_def = page_data.get("componentDefinition", {}) or {}
    root_key = page_data.get("rootComponent", "")
    root_comp = comp_def.get(root_key, {}) if root_key else {}
    top_sections = list((root_comp.get("children") or {}).keys())
    pages[key] = {
        "id": page_data.get("id", ""),
        "component_count": len(comp_def),
        "root": root_key,
        "top_sections": top_sections[:_MAX_KNOWN_COMPONENTS_PER_PAGE],
        "event_function_names": list((page_data.get("eventFunctions") or {}).keys()),
    }
    # Soft cap memory size per app
    if len(pages) > _MAX_KNOWN_PAGES_PER_APP:
        # Drop the oldest (first-inserted) entry
        oldest = next(iter(pages))
        if oldest != key:
            pages.pop(oldest, None)


def _remember_application(
    context: dict[str, Any],
    app_code: str,
    ui_app_id: str = "",
    app_type: str = "",
    page_names: list[str] | None = None,
) -> None:
    """Record application-level metadata."""
    known = _get_known(context)
    if not known:
        return
    apps = known.setdefault("applications", {})
    entry = apps.setdefault(app_code, {})
    if ui_app_id:
        entry["ui_app_id"] = ui_app_id
    if app_type:
        entry["app_type"] = app_type
    if page_names is not None:
        entry["page_names"] = page_names


def _invalidate_page_memory(context: dict[str, Any], app_code: str, page_name: str) -> None:
    """Drop cached page memo after a mutation (forces re-read to get fresh state)."""
    known = _get_known(context)
    pages = known.get("pages") if known else None
    if pages:
        pages.pop(f"{app_code}/{page_name}", None)


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

    # Remember page names for this app — avoids re-listing on later turns.
    page_names = [p.get("name", "") for p in pages if p.get("name")]
    _remember_application(context, app_code, page_names=page_names)

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
    app_code = params.get("app_code") or context.get("app_code", "")

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

    # Remember what we learned — so the agent doesn't re-fetch it later.
    # The dynamic context builder surfaces this as "Known entities" on each turn.
    _remember_page(context, app_code, page_name, page_data)

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

    # Default: COMPACT component tree (top 2 levels only, with descendant counts).
    # Pages can have hundreds of components — dumping the full tree bloats the
    # conversation history. Use include='subtree' with subtree_root to drill into
    # a specific section, or include='search' to find components by name/type.
    max_depth = params.get("max_depth", 2)
    tree = build_component_tree(page_data, max_depth=max_depth)
    comp_count = len(page_data.get("componentDefinition", {}))
    event_count = len(page_data.get("eventFunctions", {}))
    summary = (
        f"Page '{page_name}' structure ({comp_count} components, {event_count} event functions):\n\n"
        f"{tree}\n\n"
        f"Tip: use include='subtree' with subtree_root=<key> to drill into a section, "
        f"include='search' to find components, or component_key=<key> for one component."
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
    # Steps can be a dict (name → def) or a list (array of defs)
    if isinstance(steps, dict):
        step_items = steps.items()
    elif isinstance(steps, list):
        step_items = ((s.get("statementName", s.get("name", f"step_{i}")), s) for i, s in enumerate(steps))
    else:
        step_items = ()
    for step_name, step_def in step_items:
        if not isinstance(step_def, dict):
            continue
        compact_steps[step_name] = {
            "namespace": step_def.get("namespace", "?"),
            "name": step_def.get("name", "?"),
            "dependentSteps": step_def.get("dependentSteps", []),
        }
        param_map = step_def.get("parameterMap", {})
        if param_map:
            compact_steps[step_name]["parameterMapKeys"] = list(param_map.keys()) if isinstance(param_map, dict) else []
    compact = {
        "name": definition.get("name", function_name),
        "namespace": definition.get("namespace", ""),
        "steps": compact_steps,
    }
    step_count = len(steps) if isinstance(steps, (dict, list)) else 0
    return ToolResult(
        success=True,
        data=definition,
        summary=f"Event function '{function_name}' ({step_count} steps):\n{json.dumps(compact, indent=2, default=str)}",
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
    app_code = params.get("app_code") or context.get("app_code", "")

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
        op_result = _apply_component_operations(page_data, operations, context.get("catalog"))
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

    # Refresh our remembered view of the page — the IDs/top-level structure
    # may have changed. The agent will see the updated memo on the next turn.
    _remember_page(context, app_code, page_name, page_data)

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


def _apply_component_operations(
    page_data: dict, operations: list[dict], catalog: Any = None,
) -> str:
    """Apply batch component operations. Returns summary string.

    ``catalog`` is the component catalog singleton used to validate property
    and style names against each component's schema. When provided, unknown
    property names surface as WARNINGS in the summary so the LLM can
    self-correct on the next turn (the op is still applied — the backend
    remains the authoritative validator).
    """
    comp_def = page_data.setdefault("componentDefinition", {})
    root_key = page_data.get("rootComponent", "")

    errors: list[str] = []
    warnings: list[str] = []
    applied: list[str] = []

    for i, op in enumerate(operations):
        op_type = op.get("op")
        err = None

        if op_type == "add":
            err = _op_add(comp_def, op, catalog, warnings)
        elif op_type == "update":
            err = _op_update(comp_def, op, catalog, warnings)
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
    if warnings:
        parts.append(f"WARNINGS ({len(warnings)}): {'; '.join(warnings)}")
    return "; ".join(parts)


# ── Component operation handlers ──────────────────────────────────


def _op_add(
    comp_def: dict, op: dict, catalog: Any = None, warnings: list[str] | None = None,
) -> str | None:
    """Add a new component. Returns error string or None.

    Appends any schema mismatches from the catalog to ``warnings`` so the
    caller can surface them in the tool summary.
    """
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

    _collect_schema_warnings(
        catalog, warnings,
        comp_type=component_type, component_key=component_key, op_name="add",
        properties=op.get("properties"), styles=op.get("style_properties"),
    )
    return None


def _op_update(
    comp_def: dict, op: dict, catalog: Any = None, warnings: list[str] | None = None,
) -> str | None:
    """Merge properties/styles into a component. Returns error string or None.

    Appends any schema mismatches from the catalog to ``warnings`` so the
    caller can surface them in the tool summary.
    """
    component_key = op.get("component_key")
    if not component_key:
        return "update op missing component_key"
    if component_key not in comp_def:
        return f"Component '{component_key}' not found"

    comp = comp_def[component_key]
    if op.get("properties"):
        comp.setdefault("properties", {}).update(op["properties"])
    if op.get("style_properties"):
        _merge_style_properties(comp, op["style_properties"])
    if op.get("display_order") is not None:
        comp["displayOrder"] = op["display_order"]
    for bp_key, bp_value in op.get("binding_paths", {}).items():
        comp[bp_key] = bp_value

    _collect_schema_warnings(
        catalog, warnings,
        comp_type=comp.get("type", ""), component_key=component_key, op_name="update",
        properties=op.get("properties"), styles=op.get("style_properties"),
    )
    return None


def _collect_schema_warnings(
    catalog: Any,
    warnings: list[str] | None,
    *,
    comp_type: str,
    component_key: str,
    op_name: str,
    properties: dict | None,
    styles: dict | None,
) -> None:
    """Run catalog validation and append any schema mismatches to ``warnings``.

    Also emits a targeted hint for the common ``properties: {"value": {...}}``
    hallucination — ``value`` is never a property NAME (it's the inner
    ComponentProperty wrapper), so when the LLM writes it the update silently
    no-ops. We suggest the likely real property name when we can infer it
    from the component type.
    """
    if catalog is None or warnings is None:
        return

    if properties and "value" in properties:
        hint = _suggest_value_property(comp_type)
        suffix = f" — did you mean '{hint}'?" if hint else ""
        warnings.append(
            f"{op_name} '{component_key}': 'value' is NOT a property name on {comp_type or '?'}; "
            f"it's the inner ComponentProperty wrapper{suffix} "
            f"(write `properties: {{\"{hint or '<propName>'}\": {{\"value\": ...}}}}`, "
            f"not `properties: {{\"value\": {{\"value\": ...}}}}`)."
        )

    if not comp_type:
        return
    try:
        schema_warnings = catalog.validate_component(comp_type, properties, styles)
    except Exception:  # pragma: no cover — catalog is advisory, never block
        return
    for w in schema_warnings:
        warnings.append(f"{op_name} '{component_key}': {w}")


# Best-guess mapping from component type → the property name most likely
# intended when the LLM wrongly uses ``value``. Keeps the agent's self-correction
# fast without requiring a full catalog lookup.
_VALUE_PROPERTY_HINTS = {
    "Text": "text",
    "Button": "label",
    "Link": "label",
    "TextBox": "bindingPath",
    "TextArea": "bindingPath",
    "Image": "src",
    "Icon": "icon",
}


def _suggest_value_property(comp_type: str) -> str:
    """Return the likely intended property name for a type, or '' if unknown."""
    return _VALUE_PROPERTY_HINTS.get(comp_type, "")


def _merge_style_properties(comp: dict, new_styles: dict) -> None:
    """Merge new style properties into the component's existing styleProperties.

    Modlix stores styleProperties as {<groupId>: {condition?, pseudoState?, resolutions}}.
    Without a condition, there should be ONLY ONE group (the default). Pseudo states
    like :hover are encoded as suffixed prop names within that one group (e.g.
    "color:hover"), NOT as separate groups. Modlix's processing logic OVERWRITES
    non-conditioned groups instead of merging, so creating a new group ID for each
    update would lose all prior styles.

    This function:
    1. If incoming has a `condition` field → only matches existing entry with same condition
    2. Otherwise → merges into the single default (non-conditioned) group
    3. Falls back to creating a new group only if no existing one matches
    """
    import uuid as _uuid

    existing = comp.setdefault("styleProperties", {})

    for incoming_key, incoming_val in new_styles.items():
        if not isinstance(incoming_val, dict):
            existing[incoming_key] = incoming_val
            continue

        in_cond = incoming_val.get("condition")

        # If the agent passed an existing key, deep-merge into it
        if incoming_key in existing:
            _deep_merge(existing[incoming_key], incoming_val)
            continue

        # Find an existing group matching the incoming condition
        # (no condition matches no condition; otherwise must match exactly)
        target_id = None
        for ex_id, ex_val in existing.items():
            if not isinstance(ex_val, dict):
                continue
            ex_cond = ex_val.get("condition")
            if ex_cond == in_cond:
                target_id = ex_id
                break

        if target_id:
            # Deep-merge into the existing matching group
            _deep_merge(existing[target_id], incoming_val)
        else:
            # No matching group — create a new one with a UUID-like key
            new_id = _uuid.uuid4().hex[:22]
            existing[new_id] = incoming_val


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
