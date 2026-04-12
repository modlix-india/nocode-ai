"""Layout refiner — uses Gemini Flash to fix layout issues in converted pages.

Takes the programmatically extracted component tree + screenshot and asks
Gemini Flash to produce layout corrections:
- Collapse redundant nesting (div→div→div→text → text)
- Set Grid column layouts (gridTemplateColumns)
- Choose row vs column direction per section
- Set proper responsive behavior
- Fix z-index layering

Returns a list of corrections applied directly to the componentDefinition.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_REFINE_PROMPT = """\
You are a layout expert. I have a website screenshot and a component tree extracted from its HTML.
The components are in Modlix format (Grid, Text, Image, Button types).

The PROBLEM is that the layout doesn't match the screenshot because:
1. Some Grid components need column layouts (gridTemplateColumns) to arrange children side-by-side
2. Some sections need flexDirection: row instead of the default column
3. Some deeply nested empty Grids should be removed
4. Some components need specific width/height constraints

Look at the screenshot and the component tree below. Return a JSON array of layout fixes.

Each fix is: {"key": "component_key", "action": "set_style"|"set_property"|"remove", "styles": {...}}

For "set_style" action, provide camelCase CSS properties to add/override in styleProperties.
For "set_property" action, provide properties to set (like layout: "ROWLAYOUT").
For "remove" action, just provide the key — the component will be removed and children reparented.

IMPORTANT RULES:
- Grid components that have 2-4 children arranged HORIZONTALLY need: {"gridTemplateColumns": "1fr 1fr"} or similar
- The navbar should use gridTemplateColumns or flexDirection: row with justifyContent: space-between
- Hero sections need: width: 100%, minHeight: 100vh or specific height, backgroundSize: cover
- Card grids need equal column widths
- For SITE type (no themes): all colors must be inline in styles
- Use ROWLAYOUT property for horizontal Grid layouts: {"layout": {"value": "ROWLAYOUT"}}

COMPONENT TREE:
"""


async def refine_layout_with_vision(
    screenshot_base64: str,
    comp_def: dict[str, Any],
    url: str = "",
) -> list[dict[str, Any]]:
    """Use Gemini Flash to analyze screenshot and fix layout.

    Args:
        screenshot_base64: Base64 PNG of the source website.
        comp_def: Current componentDefinition dict.
        url: Source URL for context.

    Returns:
        List of fixes applied to comp_def (modified in place).
    """
    try:
        import google.generativeai as genai
        from app.config import settings

        if not settings.GOOGLE_API_KEY:
            logger.info("No GOOGLE_API_KEY — skipping layout refinement")
            return []

        genai.configure(api_key=settings.GOOGLE_API_KEY)
        model = genai.GenerativeModel("gemini-2.0-flash")
    except ImportError:
        logger.warning("google-generativeai not available for layout refinement")
        return []

    # Build a compact tree representation for the prompt
    tree_text = _build_compact_tree(comp_def)

    image_bytes = base64.b64decode(screenshot_base64)

    prompt = _REFINE_PROMPT + tree_text + "\n\nReturn ONLY a JSON array of fixes. No explanation."

    try:
        response = await asyncio.to_thread(
            model.generate_content,
            [
                {"mime_type": "image/png", "data": image_bytes},
                prompt,
            ],
        )

        response_text = response.text if response.text else "[]"
        # Extract JSON from response (may have markdown fencing)
        json_match = response_text
        if "```" in json_match:
            import re
            match = re.search(r"```(?:json)?\s*([\s\S]*?)```", json_match)
            if match:
                json_match = match.group(1)

        fixes = json.loads(json_match)
        if not isinstance(fixes, list):
            fixes = [fixes]

        logger.info("Gemini Flash suggested %d layout fixes", len(fixes))

        # Apply fixes
        applied = _apply_fixes(comp_def, fixes)
        logger.info("Applied %d layout fixes", len(applied))
        return applied

    except Exception as e:
        logger.warning("Layout refinement failed: %s", e)
        return []


def _build_compact_tree(comp_def: dict[str, Any], max_depth: int = 3) -> str:
    """Build a compact text representation of the component tree."""
    root_key = None
    for key, comp in comp_def.items():
        if key == "root":
            root_key = key
            break
    if not root_key:
        return "(empty)"

    lines: list[str] = []
    _tree_recursive(comp_def, root_key, lines, "", True, True, 0, max_depth)
    return "\n".join(lines)


def _tree_recursive(
    comp_def: dict, key: str, lines: list, prefix: str,
    is_last: bool, is_root: bool, depth: int, max_depth: int,
) -> None:
    comp = comp_def.get(key, {})
    if not comp:
        return

    ctype = comp.get("type", "?")
    children = comp.get("children", {})
    n_children = len(children)

    # Style summary
    sp = comp.get("styleProperties", {})
    style_keys = []
    for sid, sdata in sp.items():
        for k in sdata.get("resolutions", {}).get("ALL", {}):
            style_keys.append(k)

    # Property summary
    props = comp.get("properties", {})
    text = props.get("text", {}).get("value", "")[:40]
    src = props.get("src", {}).get("value", "")[:60]
    label = props.get("label", {}).get("value", "")[:30]

    info = f"{key} ({ctype})"
    if text:
        info += f' "{text}"'
    if src:
        info += f" img={src.split('/')[-1]}"
    if label:
        info += f' btn="{label}"'
    if n_children:
        info += f" [{n_children} children]"

    # Show key styles
    important = [s for s in style_keys if s in (
        "backgroundColor", "backgroundImage", "color", "fontSize",
        "height", "width", "position", "display", "flexDirection",
        "gridTemplateColumns", "justifyContent", "gap",
    )]
    if important:
        info += f" styles:{','.join(important)}"

    if is_root:
        lines.append(info)
    else:
        connector = "└── " if is_last else "├── "
        lines.append(f"{prefix}{connector}{info}")

    if depth >= max_depth:
        if n_children:
            child_prefix = prefix + ("    " if is_last else "│   ")
            lines.append(f"{child_prefix}... ({n_children} children)")
        return

    child_prefix = prefix + ("    " if is_last or is_root else "│   ")
    child_keys = list(children.keys())
    for i, child_key in enumerate(child_keys):
        _tree_recursive(
            comp_def, child_key, lines, child_prefix,
            i == len(child_keys) - 1, False, depth + 1, max_depth,
        )


def _apply_fixes(comp_def: dict[str, Any], fixes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply layout fixes to componentDefinition."""
    applied = []

    for fix in fixes:
        key = fix.get("key", "")
        action = fix.get("action", "")

        if key not in comp_def:
            continue

        comp = comp_def[key]

        if action == "set_style":
            styles = fix.get("styles", {})
            if not styles:
                continue
            # Merge into existing styleProperties
            sp = comp.get("styleProperties", {})
            if sp:
                # Find existing style entry and merge
                for sid, sdata in sp.items():
                    all_res = sdata.get("resolutions", {}).get("ALL", {})
                    for prop, val in styles.items():
                        all_res[prop] = {"value": str(val)}
                    break
            else:
                # Create new style entry
                import uuid
                style_id = uuid.uuid4().hex[:22]
                comp["styleProperties"] = {
                    style_id: {
                        "resolutions": {
                            "ALL": {prop: {"value": str(val)} for prop, val in styles.items()}
                        }
                    }
                }
            applied.append(fix)

        elif action == "set_property":
            props = fix.get("properties", {})
            for prop, val in props.items():
                if isinstance(val, dict):
                    comp.setdefault("properties", {})[prop] = val
                else:
                    comp.setdefault("properties", {})[prop] = {"value": val}
            applied.append(fix)

        elif action == "remove":
            # Reparent children to parent
            parent_key = _find_parent(comp_def, key)
            if parent_key:
                parent = comp_def[parent_key]
                parent_children = parent.get("children", {})
                # Remove this key from parent
                parent_children.pop(key, None)
                # Add this component's children to parent
                for child_key in comp.get("children", {}):
                    parent_children[child_key] = True
            del comp_def[key]
            applied.append(fix)

    return applied


def _find_parent(comp_def: dict[str, Any], target_key: str) -> str | None:
    """Find the parent of a component."""
    for key, comp in comp_def.items():
        if target_key in comp.get("children", {}):
            return key
    return None
