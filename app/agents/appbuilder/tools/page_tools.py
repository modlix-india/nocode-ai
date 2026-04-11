"""Dedicated page tools — deferred tools for surgical page navigation and editing.

These complement the core ``read_entity``/``update_entity`` tools with
page-specific operations that avoid fetching full page definitions.

All tools are deferred (discovered via ToolSearchTool).
"""

from __future__ import annotations

import json
from typing import Any

from app.core.tools.base import (
    ToolDefinition,
    ToolParameter,
    ToolResult,
    ResultTier,
)
from app.agents.appbuilder.tools._executor import (
    fetch_page_by_name,
    build_component_tree,
    build_page_summary,
    search_components,
    build_subtree,
    save_page,
)
from app.agents.appbuilder.tools.crud.page_ops import (
    _read_component,
    _read_event_function,
    _apply_component_operations,
    _apply_page_properties,
)


def _get_client_and_headers(context: dict[str, Any]):
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context["headers"]


def _resolve_app_code(params: dict[str, Any], context: dict[str, Any]) -> tuple[str, ToolResult | None]:
    app_code = params.get("app_code") or context.get("app_code", "")
    if not app_code:
        return "", ToolResult(
            success=False,
            error="No appCode set. Use list_entities(object_type='application') first.",
        )
    return app_code, None


# ── page_structure ───────────────────────────────────────────────


async def _execute_page_structure(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    """Return lightweight component tree for a page."""
    client, headers = _get_client_and_headers(context)
    page_name = params.get("page_name", "")
    app_code, err = _resolve_app_code(params, context)
    if err:
        return err
    if not page_name:
        return ToolResult(success=False, error="page_name is required.")

    page_data, error = await fetch_page_by_name(client, page_name, app_code, headers)
    if error:
        return ToolResult(success=False, error=error)

    tree = build_component_tree(page_data)
    comp_count = len(page_data.get("componentDefinition", {}))
    event_count = len(page_data.get("eventFunctions", {}))
    return ToolResult(
        success=True,
        summary=(
            f"Page '{page_name}' structure ({comp_count} components, {event_count} events):\n\n{tree}"
        ),
        result_tier=ResultTier.STANDARD,
    )


PAGE_STRUCTURE = ToolDefinition(
    name="page_structure",
    description="Get the lightweight component tree of a page — shows keys, types, and children hierarchy without properties or styles.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name.", required=True),
        ToolParameter(name="app_code", type="string", description="App code (uses session default if omitted).", required=False),
    ],
    execute=_execute_page_structure,
    is_deferred=True,
    search_hint="component tree layout hierarchy overview structure",
    result_tier=ResultTier.STANDARD,
)


# ── read_component ───────────────────────────────────────────────


async def _execute_read_component(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    """Read specific component(s) by key."""
    client, headers = _get_client_and_headers(context)
    page_name = params.get("page_name", "")
    app_code, err = _resolve_app_code(params, context)
    if err:
        return err
    if not page_name:
        return ToolResult(success=False, error="page_name is required.")

    keys = params.get("keys", [])
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.split(",")]
    if not keys:
        return ToolResult(success=False, error="keys (list of component keys) is required.")

    page_data, error = await fetch_page_by_name(client, page_name, app_code, headers)
    if error:
        return ToolResult(success=False, error=error)

    results = []
    for key in keys:
        r = _read_component(page_data, page_name, key)
        if r.success:
            results.append(r.summary or json.dumps(r.data, indent=2, default=str))
        else:
            results.append(f"[{key}]: {r.error}")

    return ToolResult(
        success=True,
        summary="\n\n".join(results),
        result_tier=ResultTier.STANDARD,
    )


READ_COMPONENT = ToolDefinition(
    name="read_component",
    description="Read one or more specific components by key from a page. Returns properties, children, and binding info (styles summarized as keys only).",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name.", required=True),
        ToolParameter(
            name="keys",
            type="array",
            description="Component keys to read.",
            required=True,
            items={"type": "string"},
        ),
        ToolParameter(name="app_code", type="string", description="App code (uses session default if omitted).", required=False),
    ],
    execute=_execute_read_component,
    is_deferred=True,
    search_hint="read specific component properties styles details",
    result_tier=ResultTier.STANDARD,
)


# ── read_event ───────────────────────────────────────────────────


async def _execute_read_event(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    """Read a single page event function."""
    client, headers = _get_client_and_headers(context)
    page_name = params.get("page_name", "")
    event_name = params.get("event_name", "")
    app_code, err = _resolve_app_code(params, context)
    if err:
        return err
    if not page_name:
        return ToolResult(success=False, error="page_name is required.")
    if not event_name:
        return ToolResult(success=False, error="event_name is required.")

    page_data, error = await fetch_page_by_name(client, page_name, app_code, headers)
    if error:
        return ToolResult(success=False, error=error)

    return _read_event_function(page_data, page_name, event_name)


READ_EVENT = ToolDefinition(
    name="read_event",
    description="Read a single page event function definition. Returns step names, namespaces, dependencies, and parameter map keys.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name.", required=True),
        ToolParameter(name="event_name", type="string", description="Event function name.", required=True),
        ToolParameter(name="app_code", type="string", description="App code (uses session default if omitted).", required=False),
    ],
    execute=_execute_read_event,
    is_deferred=True,
    search_hint="read page event function steps definition",
    result_tier=ResultTier.STANDARD,
)


# ── search_components ────────────────────────────────────────────


async def _execute_search_components(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    """Search/filter components within a page."""
    client, headers = _get_client_and_headers(context)
    page_name = params.get("page_name", "")
    app_code, err = _resolve_app_code(params, context)
    if err:
        return err
    if not page_name:
        return ToolResult(success=False, error="page_name is required.")

    page_data, error = await fetch_page_by_name(client, page_name, app_code, headers)
    if error:
        return ToolResult(success=False, error=error)

    filters = {
        "search_type": params.get("type"),
        "search_name": params.get("name"),
        "search_text": params.get("text"),
        "search_has_binding": params.get("has_binding", False),
        "search_has_events": params.get("has_events", False),
    }
    results = search_components(page_data, filters)
    if not results:
        return ToolResult(
            success=True,
            summary=f"No components matched the search filters on page '{page_name}'.",
            result_tier=ResultTier.COMPACT,
        )
    return ToolResult(
        success=True,
        data=results,
        summary=f"Found {len(results)} matching component(s):\n{json.dumps(results, indent=2, default=str)}",
        result_tier=ResultTier.LARGE,
    )


SEARCH_COMPONENTS = ToolDefinition(
    name="search_components",
    description="Search/filter components within a page by type, name, text content, bindings, or event references.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name.", required=True),
        ToolParameter(name="type", type="string", description="Component type to filter by (e.g. 'Button', 'TextBox').", required=False),
        ToolParameter(name="name", type="string", description="Substring match on component key or name.", required=False),
        ToolParameter(name="text", type="string", description="Substring match on label/text properties.", required=False),
        ToolParameter(name="has_binding", type="boolean", description="Only components with data bindings.", required=False),
        ToolParameter(name="has_events", type="boolean", description="Only components with event function references.", required=False),
        ToolParameter(name="app_code", type="string", description="App code (uses session default if omitted).", required=False),
    ],
    execute=_execute_search_components,
    is_deferred=True,
    search_hint="find filter components by type name binding events",
    result_tier=ResultTier.LARGE,
)


# ── patch_components ─────────────────────────────────────────────


async def _execute_patch_components(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    """Surgical batch component operations (add/update/remove/move)."""
    client, headers = _get_client_and_headers(context)
    page_name = params.get("page_name", "")
    app_code, err = _resolve_app_code(params, context)
    if err:
        return err
    if not page_name:
        return ToolResult(success=False, error="page_name is required.")

    operations = params.get("operations", [])
    if not operations:
        return ToolResult(success=False, error="operations array is required.")

    page_data, error = await fetch_page_by_name(client, page_name, app_code, headers)
    if error:
        return ToolResult(success=False, error=error)

    op_result = _apply_component_operations(page_data, operations)

    page_data["message"] = params.get("message", "Component update via patch_components")
    save_result = await save_page(
        client, page_data["id"], page_data, headers, context.get("client_code", ""),
    )
    if not save_result.success:
        return save_result

    return ToolResult(
        success=True,
        summary=f"Updated page '{page_name}': {op_result}",
        result_tier=ResultTier.COMPACT,
    )


PATCH_COMPONENTS = ToolDefinition(
    name="patch_components",
    description=(
        "Surgical batch component operations on a page. Supports: "
        "add (new component under parent), update (merge properties/styles), "
        "remove (delete component and descendants), move (reparent component). "
        "Combine multiple operations in one call for efficiency."
    ),
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name.", required=True),
        ToolParameter(
            name="operations",
            type="array",
            description=(
                "Array of operations. Each: "
                '{op:"add", parent_key, component_key, type, properties?, style_properties?}, '
                '{op:"update", component_key, properties?, style_properties?}, '
                '{op:"remove", component_key, recursive?}, '
                '{op:"move", component_key, new_parent_key, display_order?}'
            ),
            required=True,
            items={"type": "object"},
        ),
        ToolParameter(name="message", type="string", description="Change description for version history.", required=True),
        ToolParameter(name="app_code", type="string", description="App code (uses session default if omitted).", required=False),
    ],
    execute=_execute_patch_components,
    is_deferred=True,
    search_hint="edit add update remove move components surgical batch",
    result_tier=ResultTier.COMPACT,
)


# ── patch_event ──────────────────────────────────────────────────


async def _execute_patch_event(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    """Write or update a single page event function."""
    client, headers = _get_client_and_headers(context)
    page_name = params.get("page_name", "")
    event_name = params.get("event_name", "")
    definition = params.get("definition", {})
    app_code, err = _resolve_app_code(params, context)
    if err:
        return err
    if not page_name:
        return ToolResult(success=False, error="page_name is required.")
    if not event_name:
        return ToolResult(success=False, error="event_name is required.")
    if not definition:
        return ToolResult(success=False, error="definition is required.")

    page_data, error = await fetch_page_by_name(client, page_name, app_code, headers)
    if error:
        return ToolResult(success=False, error=error)

    page_data.setdefault("eventFunctions", {})[event_name] = definition

    steps = definition.get("steps", {})
    step_count = len(steps) if isinstance(steps, dict) else 0

    page_data["message"] = params.get("message", f"Updated event function '{event_name}'")
    save_result = await save_page(
        client, page_data["id"], page_data, headers, context.get("client_code", ""),
    )
    if not save_result.success:
        return save_result

    return ToolResult(
        success=True,
        summary=f"Wrote event function '{event_name}' ({step_count} steps) on page '{page_name}'.",
        result_tier=ResultTier.COMPACT,
    )


PATCH_EVENT = ToolDefinition(
    name="patch_event",
    description="Write or update a single page event function. Provide the full event function definition (steps, parameters, events).",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name.", required=True),
        ToolParameter(name="event_name", type="string", description="Event function name.", required=True),
        ToolParameter(name="definition", type="object", description="Full event function definition (steps, name, namespace).", required=True),
        ToolParameter(name="message", type="string", description="Change description for version history.", required=True),
        ToolParameter(name="app_code", type="string", description="App code (uses session default if omitted).", required=False),
    ],
    execute=_execute_patch_event,
    is_deferred=True,
    search_hint="write update page event function handler logic",
    result_tier=ResultTier.COMPACT,
)


# ── read_subtree ─────────────────────────────────────────────────


async def _execute_read_subtree(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    """Read a branch of the component tree with inline property details."""
    client, headers = _get_client_and_headers(context)
    page_name = params.get("page_name", "")
    subtree_root = params.get("subtree_root", "")
    app_code, err = _resolve_app_code(params, context)
    if err:
        return err
    if not page_name:
        return ToolResult(success=False, error="page_name is required.")
    if not subtree_root:
        return ToolResult(success=False, error="subtree_root (component key) is required.")

    page_data, error = await fetch_page_by_name(client, page_name, app_code, headers)
    if error:
        return ToolResult(success=False, error=error)

    return ToolResult(
        success=True,
        summary=build_subtree(page_data, subtree_root),
        result_tier=ResultTier.STANDARD,
    )


READ_SUBTREE = ToolDefinition(
    name="read_subtree",
    description="Read a branch of the component tree with inline property summaries, binding info, and event annotations.",
    parameters=[
        ToolParameter(name="page_name", type="string", description="Page name.", required=True),
        ToolParameter(name="subtree_root", type="string", description="Component key of the subtree root.", required=True),
        ToolParameter(name="app_code", type="string", description="App code (uses session default if omitted).", required=False),
    ],
    execute=_execute_read_subtree,
    is_deferred=True,
    search_hint="read component section subtree branch children detailed",
    result_tier=ResultTier.STANDARD,
)


# ── Exports ──────────────────────────────────────────────────────

PAGE_TOOLS: list[ToolDefinition] = [
    PAGE_STRUCTURE,
    READ_COMPONENT,
    READ_EVENT,
    SEARCH_COMPONENTS,
    PATCH_COMPONENTS,
    PATCH_EVENT,
    READ_SUBTREE,
]
