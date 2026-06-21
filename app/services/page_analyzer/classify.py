"""Component classification: tag map + heuristic recognition.

Tag->component mapping is applied in-browser (segment.py `_WALK_JS`); this module
refines that with heuristics over class names, ARIA roles, and structure so that
div-built widgets (buttons, tabs, carousels, dropdowns, navs) are recognized
rather than left as bare Grids. No LLM.
"""

from __future__ import annotations

from typing import Any, Dict

from app.services.page_analyzer.models import ComponentNode

# Modlix component types we may upgrade to (must exist in VALID_COMPONENT_TYPES).
_UPGRADE_OK = {"Button", "Tabs", "Carousel", "Menu", "Dropdown", "Gallery"}


def refine_component_type(node: ComponentNode, raw: Dict[str, Any]) -> ComponentNode:
    """Upgrade a node's component_type / set a recognition hint from heuristics."""
    ct = node.component_type
    cls = " ".join(node.classes).lower()
    role = (node.role_attr or "").lower()
    tag = node.tag
    child_count = int(raw.get("child_count") or 0)

    def has(*words: str) -> bool:
        return any(w in cls for w in words)

    # Button-like (div/link styled as a button, or role=button).
    if (role == "button" or has("btn", "button")) and ct in ("Grid", "Link"):
        node.component_type = "Button"
        node.recognized_as = "button-like"

    # Tabs.
    if (role == "tablist" or has("tabs", "tab-list")) and ct == "Grid":
        node.component_type = "Tabs"
        node.recognized_as = "tabs"

    # Carousel / slider.
    if has("carousel", "slider", "swiper", "slick", "embla") and ct == "Grid":
        node.component_type = "Carousel"
        node.recognized_as = "carousel"

    # Navigation menu.
    if role in ("navigation", "menubar", "menu") or has("navbar", "nav-menu"):
        node.recognized_as = node.recognized_as or "nav"

    # Dropdown / select built from divs.
    if role in ("combobox", "listbox") or has("dropdown", "select-menu"):
        node.recognized_as = node.recognized_as or "dropdown"

    # Repeated-children container (gallery/grid of cards) — hint only.
    if ct == "Grid" and child_count >= 4 and node.recognized_as is None:
        node.recognized_as = "repeated-children"

    # Icon-only link/button (no own text but has an svg/img child).
    if tag in ("a", "button") and not node.text and child_count >= 1:
        node.recognized_as = node.recognized_as or "icon-only"

    if node.component_type not in _UPGRADE_OK and node.component_type == "":
        node.component_type = "Grid"
    return node
