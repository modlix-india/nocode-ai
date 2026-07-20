"""Component-catalog tools — list types, fetch full schema, build examples.

Ported from modlix-mcp/modlix_mcp/tools/components.py. Catalog access goes
through `app.agents.appbuilder.catalog.get_catalog()` (the singleton set up
by main.py lifespan); when the catalog hasn't loaded yet we surface a clear
error rather than crash.

The composition tools (add_component, move_component, set_styles, …) live
in the `pages.py` port — same file because most of them mutate page JSON
and benefit from sharing helpers with the page tools.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult


def _get_catalog():
    """Return the catalog singleton; raises if not yet loaded.

    The lifespan registers the catalog via `set_catalog` after `await
    catalog.load()`. If a tool is invoked before that ran (unit tests,
    scripts) we want a clear failure mode rather than empty results.
    """
    from app.agents.appbuilder.catalog import get_catalog
    return get_catalog()


def _catalog_components(cat: Any) -> dict[str, Any]:
    """Return the components dict from the catalog, or {} if not yet loaded."""
    raw = getattr(cat, "_catalog", None) or {}
    return raw.get("components") or {}


# ── list_component_types ─────────────────────────────────────────────────


async def _execute_list_component_types(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    cat = _get_catalog()
    components = _catalog_components(cat)
    if not components:
        return ToolResult(
            success=False,
            error=(
                "Component catalog is empty. Either the CDN load failed "
                "or the lifespan hasn't registered it yet (set_catalog). "
                "Check logs for 'Loading component catalog' on startup."
            ),
        )
    tier = (params.get("tier") or "").strip().lower() or None
    items: list[dict[str, Any]] = []
    for name, info in sorted(components.items()):
        t = info.get("tier", "specialized")
        if tier and t != tier:
            continue
        items.append({
            "type": name,
            "tier": t,
            "description": info.get("description", ""),
        })
    return ToolResult(
        success=True,
        summary=f"{len(items)} components:\n{json.dumps(items, indent=2, default=str)}",
    )


list_component_types_tool = ToolDefinition(
    name="list_component_types",
    description=(
        "List every Modlix component type the catalog knows about, with its "
        "tier (common/data/table/specialized/multimedia) and one-line "
        "description. Use to pick the right component before add_component. "
        "Optional tier filter narrows to a category."
    ),
    parameters=[
        ToolParameter(
            name="tier", type="string", required=False,
            description="Filter by tier: 'common', 'data', 'table', 'specialized', 'multimedia'. Omit for all.",
        ),
    ],
    execute=_execute_list_component_types,
)


# ── get_component_schema ─────────────────────────────────────────────────


async def _execute_get_component_schema(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    component_type = (params.get("component_type") or "").strip()
    if not component_type:
        return ToolResult(success=False, error="`component_type` is required")
    cat = _get_catalog()
    info = cat.get_component_info(component_type)
    if not info:
        available = ", ".join(cat.get_all_types()) or "(catalog empty)"
        return ToolResult(
            success=False,
            error=f"Unknown component type '{component_type}'. Available: {available}",
        )

    # Trim payload to what a tool-using agent actually needs.
    view = {
        "type": component_type,
        "tier": info.get("tier"),
        "description": info.get("description"),
        "structure": info.get("structure"),
        "properties": [
            {
                "name": p.get("name"),
                "type": p.get("type"),
                "enum": [e.get("name") for e in (p.get("enumValues") or [])] or None,
                "description": p.get("description"),
            }
            for p in (info.get("properties") or [])
        ],
        "subComponents": list((info.get("subComponents") or {}).keys()) or None,
        "designTypes": (info.get("themeStyleProperties") or {}).get("designTypes"),
    }
    return ToolResult(success=True, summary=json.dumps(view, indent=2, default=str))


get_component_schema_tool = ToolDefinition(
    name="get_component_schema",
    description=(
        "Return the full catalog schema for one component type: properties "
        "(name + type + enum + description), style properties / design types, "
        "and sub-components. Call before add_component or "
        "update_component_props to discover valid keys for that type."
    ),
    parameters=[
        ToolParameter(
            name="component_type", type="string",
            description="Component type name (e.g. 'Button', 'TextBox', 'Table').",
        ),
    ],
    execute=_execute_get_component_schema,
)


# ── get_component_examples ───────────────────────────────────────────────


def _example_for(component_type: str, info: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal example component entry using common defaults."""
    properties: dict[str, Any] = {}
    for p in (info.get("properties") or [])[:4]:
        name = p.get("name")
        if not name:
            continue
        if p.get("enumValues"):
            properties[name] = {"value": p["enumValues"][0].get("name")}
        elif p.get("type") == "string":
            properties[name] = {"value": f"<{name}>"}
        elif p.get("type") in ("number", "integer"):
            properties[name] = {"value": 0}
        elif p.get("type") == "boolean":
            properties[name] = {"value": False}
        elif p.get("type") == "array":
            properties[name] = {"value": []}
        elif p.get("type") == "object":
            properties[name] = {"value": {}}
    return {
        "key": "<uuid>",
        "type": component_type,
        "name": component_type.lower(),
        "displayOrder": 0,
        "children": {},
        "properties": properties,
        "styleProperties": {},
    }


async def _execute_get_component_examples(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    component_type = (params.get("component_type") or "").strip()
    if not component_type:
        return ToolResult(success=False, error="`component_type` is required")
    cat = _get_catalog()
    info = cat.get_component_info(component_type)
    if not info:
        return ToolResult(
            success=False,
            error=f"Unknown component type '{component_type}'. Use list_component_types.",
        )
    example = _example_for(component_type, info)
    return ToolResult(success=True, summary=json.dumps(example, indent=2, default=str))


get_component_examples_tool = ToolDefinition(
    name="get_component_examples",
    description=(
        "Return a minimal example componentDefinition entry for the given "
        "component type — already in Modlix's wrapped property shape "
        "({value: ...}). Drop it into a page's componentDefinition map or "
        "use as a template for add_component args."
    ),
    parameters=[
        ToolParameter(
            name="component_type", type="string",
            description="Component type name to build an example for.",
        ),
    ],
    execute=_execute_get_component_examples,
)


# ── Module export ────────────────────────────────────────────────────────


TOOLS: list[ToolDefinition] = [
    list_component_types_tool,
    get_component_schema_tool,
    get_component_examples_tool,
]
