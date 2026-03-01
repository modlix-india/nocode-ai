"""Shared page read-modify-write executor.

All page and component tools go through these helpers to avoid
exposing the full 30K+ page JSON to the LLM.

Workflow:
1. fetch_page() — GET page by name+appCode
2. Modify componentDefinition in Python
3. save_page() — PUT modified page back

The executor handles version management (optimistic locking).
"""

from __future__ import annotations

import logging
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
