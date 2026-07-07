"""Shared page read-modify-write executor.

All page and component tools go through these helpers to avoid
exposing the full 30K+ page JSON to the LLM.

Workflow:
1. fetch_page() - GET page by name+appCode
2. Modify componentDefinition in Python
3. save_page() - PUT modified page back

The executor handles version management (optimistic locking).
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from app.core.tools.base import ToolResult
from app.core.tools.http_client import SaasClient

logger = logging.getLogger(__name__)

API_PREFIX = "/api/ui/pages"


async def fetch_page_by_name(
    client: SaasClient,
    page_name: str,
    app_code: str,
    headers: dict[str, str],
) -> tuple[dict[str, Any] | None, str]:
    """Fetch a full page by name and appCode.

    The list endpoint returns lightweight objects without componentDefinition.
    We first list to find the page ID, then fetch the full page by ID.

    Returns:
        Tuple of (page_dict, error_message). On success error is empty.
    """
    if not app_code:
        return None, "No appCode set. Use list_applications first to determine the appCode."

    # Step 1: find the page ID via the list endpoint
    result = await client.get(
        API_PREFIX,
        headers=headers,
        params={"page": 0, "size": 1, "appCode": app_code, "name": page_name},
    )

    if not result.success:
        return None, f"Failed to fetch page '{page_name}': {result.error}"

    data = result.data
    content = data.get("content", []) if isinstance(data, dict) else []
    if not content:
        return None, f"Page '{page_name}' not found"

    page_id = content[0].get("id")
    if not page_id:
        return None, f"Page '{page_name}' has no ID"

    # Step 2: fetch the full page definition by ID
    return await fetch_page_by_id(client, page_id, headers)


async def fetch_page_by_id(
    client: SaasClient,
    page_id: str,
    headers: dict[str, str],
) -> tuple[dict[str, Any] | None, str]:
    """Fetch a page by its MongoDB ID.

    Returns:
        Tuple of (page_dict, error_message). On success error is empty.
    """
    result = await client.get(f"{API_PREFIX}/{page_id}", headers=headers)

    if not result.success:
        return None, f"Failed to fetch page ID '{page_id}': {result.error}"

    return result.data, ""


async def save_page(
    client: SaasClient,
    page_id: str,
    page_data: dict[str, Any],
    headers: dict[str, str],
    user_client_code: str = "",
) -> ToolResult:
    """Save a modified page back to the server.

    If the page belongs to a different client, strips ``id`` and POSTs
    to create an override.  Otherwise, PUTs the full page object.
    Version is checked server-side.

    Returns:
        ToolResult indicating success or failure.
    """
    from app.agents.appbuilder.tools._shared import save_entity
    return await save_entity(client, API_PREFIX, page_id, page_data, headers, user_client_code)


def build_component_tree(page_data: dict[str, Any]) -> str:
    """Build a human-readable component tree from a page definition.

    Returns an indented tree like:
        root (Grid)
        ├── header (Grid)
        │   ├── logo (Image)
        │   └── nav (Grid)
        ├── content (Grid)
        │   ├── title (Text)
        │   └── form (Grid)
        │       ├── emailField (TextBox)
        │       └── submitBtn (Button)
        └── footer (Grid)
    """
    comp_def = page_data.get("componentDefinition", {})
    root_key = page_data.get("rootComponent", "")

    if not root_key or root_key not in comp_def:
        return "(empty page)"

    lines: list[str] = []
    _build_tree_recursive(comp_def, root_key, lines, prefix="", is_last=True, is_root=True)
    return "\n".join(lines)


def _build_tree_recursive(
    comp_def: dict[str, Any],
    key: str,
    lines: list[str],
    prefix: str,
    is_last: bool,
    is_root: bool,
) -> None:
    """Recursively build tree lines."""
    comp = comp_def.get(key, {})
    comp_type = comp.get("type", "?")

    # Build the line
    if is_root:
        connector = ""
        child_prefix = ""
    else:
        connector = "└── " if is_last else "├── "
        child_prefix = prefix + ("    " if is_last else "│   ")

    # Show key properties inline
    props = comp.get("properties", {})
    label = _get_label(props)
    extra = f' "{label}"' if label else ""

    lines.append(f"{prefix}{connector}{key} ({comp_type}){extra}")

    # Process children
    children = comp.get("children", {})
    child_keys = [k for k, v in children.items() if v is True] if isinstance(children, dict) else []

    for i, child_key in enumerate(child_keys):
        is_last_child = i == len(child_keys) - 1
        _build_tree_recursive(comp_def, child_key, lines, child_prefix, is_last_child, False)


def _get_label(properties: dict[str, Any]) -> str:
    """Extract a short label from component properties for tree display."""
    # Try common label properties
    for prop_name in ("label", "text", "placeholder", "name"):
        val = properties.get(prop_name)
        if isinstance(val, dict):
            val = val.get("value", "")
        if isinstance(val, str) and val:
            return val[:40]
    return ""


def summarize_component(comp: dict[str, Any], key: str) -> dict[str, Any]:
    """Create a compact summary of a component (for tool results)."""
    props = comp.get("properties", {})
    children = comp.get("children", {})
    child_keys = [k for k, v in children.items() if v is True] if isinstance(children, dict) else []

    return {
        "key": key,
        "type": comp.get("type", "?"),
        "properties": {k: _simplify_value(v) for k, v in props.items()},
        "children": child_keys,
        "hasStyles": bool(comp.get("styleProperties")),
    }


def _simplify_value(val: Any) -> Any:
    """Simplify a property value for display (remove deep nesting)."""
    if isinstance(val, dict):
        if "value" in val and len(val) == 1:
            return val["value"]
        if "value" in val:
            return val["value"]
    return val


# ── Page Navigator helpers ───────────────────────────────────────


def _count_descendants(comp_def: dict[str, Any], key: str) -> int:
    """Count all descendants of a component."""
    comp = comp_def.get(key, {})
    children = comp.get("children", {})
    count = 0
    for child_key, active in children.items():
        if active and child_key in comp_def:
            count += 1 + _count_descendants(comp_def, child_key)
    return count


def _build_parent_map(comp_def: dict[str, Any]) -> dict[str, str]:
    """Build a reverse lookup: child_key -> parent_key."""
    parent_map: dict[str, str] = {}
    for key, comp in comp_def.items():
        for child_key, active in comp.get("children", {}).items():
            if active:
                parent_map[child_key] = key
    return parent_map


def _get_event_refs(event_fns: dict[str, Any]) -> dict[str, list[str]]:
    """Build a map of component_key -> [event_function_names] by scanning event names."""
    refs: dict[str, list[str]] = {}
    for fn_name in event_fns:
        # Event names often follow pattern: componentKey_eventType
        parts = fn_name.rsplit("_", 1)
        if len(parts) == 2:
            comp_key = parts[0]
            refs.setdefault(comp_key, []).append(fn_name)
    return refs


def _truncate(s: str, max_len: int = 50) -> str:
    """Truncate a string with ellipsis."""
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


def _format_val(v: Any) -> str:
    """Format a value for inline display."""
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, bool):
        return str(v).lower()
    if v is None:
        return "null"
    return str(v)


def _capped_list(label: str, items: list[str], cap: int) -> str:
    """Format a labeled list, capping at `cap` items with a '...N more' suffix."""
    if len(items) <= cap:
        return f"{label}: {', '.join(items)}"
    return f"{label}: {', '.join(items[:cap])}, ...{len(items) - cap} more"


def _has_binding(comp: dict[str, Any]) -> bool:
    """Check whether a component has any data binding."""
    return bool(comp.get("bindingPath") or comp.get("bindingPath2"))


def _simplify_props(props: dict[str, Any], max_props: int = 5) -> str:
    """Simplify a properties dict into a compact inline string."""
    simplified: dict[str, Any] = {}
    for pname, pval in props.items():
        sv = _simplify_value(pval)
        if isinstance(sv, str):
            sv = _truncate(sv, 40)
        simplified[pname] = sv
    if not simplified:
        return ""
    entries = list(simplified.items())[:max_props]
    prop_str = ", ".join(f"{k}: {_format_val(v)}" for k, v in entries)
    if len(simplified) > max_props:
        prop_str += f", ...{len(simplified) - max_props} more"
    return f" props: {{{prop_str}}}"


def _format_event_annotation(evts: list[str]) -> str:
    """Format event references for inline annotation."""
    if len(evts) <= 3:
        return f"events: {', '.join(evts)}"
    return f"events: {', '.join(evts[:2])}, +{len(evts) - 2}"


def _component_annotations(comp: dict[str, Any], key: str, event_refs: dict[str, list[str]]) -> str:
    """Build bracket-enclosed annotation string for a component."""
    annotations: list[str] = []
    if _has_binding(comp):
        annotations.append("has binding")
    if key in event_refs:
        annotations.append(_format_event_annotation(event_refs[key]))
    if comp.get("styleProperties"):
        annotations.append("has styles")
    return f" [{'; '.join(annotations)}]" if annotations else ""


# ── Summary ──────────────────────────────────────────────────────


def _summary_sections(comp_def: dict[str, Any], root_key: str, event_refs: dict[str, list[str]]) -> list[str]:
    """Build lines describing top-level sections (root's direct children)."""
    root = comp_def.get(root_key, {})
    root_children = root.get("children", {})
    if not root_children:
        return []

    lines = ["Top-level sections (children of root):"]
    for child_key, active in root_children.items():
        if not active or child_key not in comp_def:
            continue
        child = comp_def[child_key]
        desc_count = _count_descendants(comp_def, child_key)
        child_type = child.get("type", "?")
        label = _get_label(child.get("properties", {}))
        label_str = f' "{label}"' if label else ""
        event_str = ", has events" if child_key in event_refs else ""
        lines.append(f"  - {child_key} ({child_type}){label_str} - {desc_count} descendants{event_str}")
    lines.append("")
    return lines


def build_page_summary(page_data: dict[str, Any]) -> str:
    """Build a condensed page overview index (PageIndex-style)."""
    comp_def = page_data.get("componentDefinition", {})
    event_fns = page_data.get("eventFunctions", {})
    root_key = page_data.get("rootComponent", "")
    page_name = page_data.get("name", "?")
    event_refs = _get_event_refs(event_fns)

    lines: list[str] = [
        f"Page '{page_name}' - {len(comp_def)} components, {len(event_fns)} event functions",
        "",
    ]

    # Component type counts
    type_counts = Counter(c.get("type", "?") for c in comp_def.values())
    lines.append(f"Component types: {', '.join(f'{t}({n})' for t, n in type_counts.most_common())}")
    lines.append("")

    # Top-level sections
    lines.extend(_summary_sections(comp_def, root_key, event_refs))

    # Event function names
    if event_fns:
        lines.append(_capped_list("Event functions", list(event_fns.keys()), 10))
        lines.append("")

    # Components with bindings
    bound_keys = [k for k, c in comp_def.items() if _has_binding(c)]
    if bound_keys:
        lines.append(_capped_list("Components with bindings", bound_keys, 10))
        lines.append("")

    # Labeled components
    text_comps = [
        f"{k}={_truncate(lbl, 30)}"
        for k, c in comp_def.items()
        if (lbl := _get_label(c.get("properties", {})))
    ]
    if text_comps:
        lines.append(_capped_list("Labeled components", text_comps, 15))

    return "\n".join(lines)


# ── Search ───────────────────────────────────────────────────────


def _matches_filters(
    key: str,
    comp: dict[str, Any],
    search_type: str,
    search_name: str,
    search_text: str,
    search_has_binding: bool,
    search_has_events: bool,
    event_refs: dict[str, list[str]],
) -> bool:
    """Return True if a component passes all active search filters."""
    if search_type and comp.get("type", "?").lower() != search_type:
        return False
    if search_name:
        comp_name = comp.get("name", key).lower()
        if search_name not in key.lower() and search_name not in comp_name:
            return False
    if search_text:
        label = _get_label(comp.get("properties", {})).lower()
        if search_text not in label:
            return False
    if search_has_binding and not _has_binding(comp):
        return False
    if search_has_events and key not in event_refs:
        return False
    return True


def _build_search_result(
    key: str,
    comp: dict[str, Any],
    parent_map: dict[str, str],
    event_refs: dict[str, list[str]],
) -> dict[str, Any]:
    """Build a condensed result dict for a matching component."""
    comp_name = comp.get("name", key)
    result: dict[str, Any] = {
        "key": key,
        "type": comp.get("type", "?"),
        "parent": parent_map.get(key, "(root)"),
    }
    if comp_name != key:
        result["name"] = comp_name
    label = _get_label(comp.get("properties", {}))
    if label:
        result["label"] = label
    if _has_binding(comp):
        result["has_binding"] = True
    if key in event_refs:
        result["events"] = event_refs[key]
    if comp.get("styleProperties"):
        result["has_styles"] = True
    child_count = sum(1 for v in comp.get("children", {}).values() if v)
    if child_count:
        result["children_count"] = child_count
    return result


def search_components(
    page_data: dict[str, Any],
    filters: dict[str, Any],
) -> list[dict[str, Any]]:
    """Search/filter components by type, name, text, bindings, events."""
    comp_def = page_data.get("componentDefinition", {})
    event_fns = page_data.get("eventFunctions", {})
    parent_map = _build_parent_map(comp_def)
    event_refs = _get_event_refs(event_fns)

    search_type = (filters.get("search_type") or "").strip().lower()
    search_name = (filters.get("search_name") or "").strip().lower()
    search_text = (filters.get("search_text") or "").strip().lower()
    search_has_binding = filters.get("search_has_binding", False)
    search_has_events = filters.get("search_has_events", False)

    return [
        _build_search_result(key, comp, parent_map, event_refs)
        for key, comp in comp_def.items()
        if _matches_filters(key, comp, search_type, search_name, search_text, search_has_binding, search_has_events, event_refs)
    ]


# ── Subtree ──────────────────────────────────────────────────────


def build_subtree(
    page_data: dict[str, Any],
    subtree_root: str,
) -> str:
    """Build a detailed subtree view with inline property summaries."""
    comp_def = page_data.get("componentDefinition", {})
    event_fns = page_data.get("eventFunctions", {})

    if subtree_root not in comp_def:
        return f"Component '{subtree_root}' not found."

    event_refs = _get_event_refs(event_fns)
    desc_count = _count_descendants(comp_def, subtree_root)
    root_type = comp_def[subtree_root].get("type", "?")

    lines: list[str] = [
        f"Subtree of '{subtree_root}' ({root_type}) - {desc_count + 1} components:",
        "",
    ]
    _build_subtree_recursive(comp_def, event_refs, subtree_root, lines, prefix="", is_last=True, is_root=True)
    return "\n".join(lines)


def _build_subtree_recursive(
    comp_def: dict[str, Any],
    event_refs: dict[str, list[str]],
    key: str,
    lines: list[str],
    prefix: str,
    is_last: bool,
    is_root: bool,
) -> None:
    """Recursively build subtree lines with condensed property info."""
    comp = comp_def.get(key, {})
    comp_type = comp.get("type", "?")

    if is_root:
        connector = ""
        child_prefix = ""
    else:
        connector = "└── " if is_last else "├── "
        child_prefix = prefix + ("    " if is_last else "│   ")

    line = f"{prefix}{connector}{key} ({comp_type})"
    line += _simplify_props(comp.get("properties", {}))
    line += _component_annotations(comp, key, event_refs)
    lines.append(line)

    # Process children
    children = comp.get("children", {})
    child_keys = [k for k, v in children.items() if v is True] if isinstance(children, dict) else []
    for i, child_key in enumerate(child_keys):
        _build_subtree_recursive(comp_def, event_refs, child_key, lines, child_prefix, i == len(child_keys) - 1, False)
