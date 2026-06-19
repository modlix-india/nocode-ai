"""Type-safe Craft block builders.

Each function produces a dict that exactly matches the JSON schema consumed
by ``CraftRenderer.tsx``.  Using these instead of raw dict literals eliminates
typo-class bugs (``"accordian"`` vs ``"accordion"``, ``isOpen`` vs ``is_open``)
and gives a single place to update if the frontend contract ever changes.

Supported block types (mirrored from CraftRenderer.tsx):
  heading, divider, badge, text, callout, metric, row, key_value,
  accordion, tabs, table, suggestion_list
"""

from __future__ import annotations

from typing import Any, Literal, Optional


# Atomic blocks


def heading_block(text: str, level: int = 1) -> dict[str, Any]:
    """Section heading.  ``level`` maps to ``<h1>``…``<h4>`` in the frontend."""
    return {"type": "heading", "text": text, "level": level}


def divider_block() -> dict[str, Any]:
    """Horizontal rule separator."""
    return {"type": "divider"}


def badge_block(label: str, variant: str = "neutral") -> dict[str, Any]:
    """Inline badge chip (e.g. health-score pill)."""
    return {"type": "badge", "label": label, "variant": variant}


def text_block(content: str) -> dict[str, Any]:
    """Free-form text paragraph."""
    return {"type": "text", "content": content}


def callout_block(
    text: str,
    variant: Literal["info", "success", "warning", "danger"] = "info",
) -> dict[str, Any]:
    """Coloured callout banner with icon."""
    return {"type": "callout", "text": text, "variant": variant}


# Data display blocks


def metric_block(
    label: str,
    value: str,
    detail: str = "",
    trend: Optional[Literal["up", "down"]] = None,
) -> dict[str, Any]:
    """Single KPI card.  ``trend`` adds an arrow indicator."""
    block: dict[str, Any] = {
        "type": "metric",
        "label": label,
        "value": value,
        "detail": detail,
    }
    if trend is not None:
        block["trend"] = trend
    return block


def row_block(children: list[dict[str, Any]]) -> dict[str, Any]:
    """Horizontal layout container for metric cards."""
    return {"type": "row", "children": children}


def key_value_block(items: list[dict[str, str]]) -> dict[str, Any]:
    """Key-value pair list.  Each item: ``{"key": str, "value": str}``."""
    return {"type": "key_value", "items": items}


def table_block(
    headers: list[str],
    rows: list[list[str]],
) -> dict[str, Any]:
    """Data table with column headers."""
    return {"type": "table", "headers": headers, "rows": rows}


# Composite / container blocks


def accordion_block(
    title: str,
    blocks: list[dict[str, Any]],
    is_open: bool = False,
) -> dict[str, Any]:
    """Collapsible accordion section containing nested blocks."""
    return {"type": "accordion", "title": title, "blocks": blocks, "isOpen": is_open}


def tabs_block(tabs: list[dict[str, Any]]) -> dict[str, Any]:
    """Tab container.  Each tab: ``{"label": str, "blocks": list[dict]}``."""
    return {"type": "tabs", "tabs": tabs}


# Suggestion list blocks


def suggestion_item(
    item_id: str,
    label: str,
    sublabel: str,
    action: str,
    reason: str,
) -> dict[str, Any]:
    """Single actionable suggestion within a suggestion section."""
    return {
        "id": item_id,
        "label": label,
        "sublabel": sublabel,
        "action": action,
        "reason": reason,
    }


def suggestion_section(title: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Group of related suggestions (e.g. "Keywords", "Age Range")."""
    return {"title": title, "items": items}


def suggestion_list_block(
    campaign_id: str,
    campaign_name: str,
    sections: list[dict[str, Any]],
) -> dict[str, Any]:
    """Top-level suggestion list widget with campaign context."""
    return {
        "type": "suggestion_list",
        "campaignId": campaign_id,
        "campaignName": campaign_name,
        "sections": sections,
    }
