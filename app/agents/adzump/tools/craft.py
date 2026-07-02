"""Craft panel builder — assembles and emits the campaign side-panel.

Single responsibility: turn product_data + competitive + geo-target state
into the ordered list of craft blocks and push them to the event stream.

Imported by product.py, geo/agent.py, and competitor.py — none of those
modules contain rendering logic directly.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def render_competitors(
    blocks: list[dict],
    competitive: dict,
    *,
    include_headers: bool = True,
) -> None:
    """Append competitor cards to an existing blocks list."""
    competitors = competitive.get("competitors") or []
    valid: list[dict] = [
        c
        for c in competitors[:20]
        if isinstance(c, dict) and (c.get("name") or "").strip()
    ]
    if not valid:
        return

    if include_headers:
        blocks.append({"type": "divider"})
        blocks.append({"type": "heading", "text": "Competitors"})

    for c in valid:
        blocks.append({"type": "heading", "text": c.get("name") or "?", "level": 2})
        kv_items: list[dict] = []
        if c.get("url"):
            kv_items.append({"key": "Website", "value": str(c["url"])})
        if c.get("business_type"):
            kv_items.append({"key": "Format", "value": str(c["business_type"])})
        if c.get("location"):
            kv_items.append({"key": "Location", "value": str(c["location"])})
        if c.get("pricing"):
            kv_items.append({"key": "Pricing", "value": str(c["pricing"])})
        key_usps = c.get("key_usps") or []
        if isinstance(key_usps, list) and key_usps:
            kv_items.append(
                {"key": "USPs", "value": ", ".join(str(u) for u in key_usps[:3])}
            )
        if c.get("weakness"):
            kv_items.append({"key": "Gap", "value": str(c["weakness"])})
        if kv_items:
            blocks.append({"type": "key_value", "items": kv_items})
        if c.get("why_competitor"):
            blocks.append({"type": "text", "content": str(c["why_competitor"])})


async def emit_craft_panel(
    stream,
    craft_id: str,
    url: str,
    business: dict,
    competitive: dict,
    screenshot_url: str | None = None,
    baked_summary: str | None = None,
    platform: str = "",
) -> None:
    """Rebuild the full campaign craft panel.

    Map section is suppressed until `platform` is set — avoids a stale
    render followed by a re-render once geo-targeting resolves.
    """
    loc = business.get("location") or ""
    if isinstance(loc, dict):
        loc = loc.get("location", "")

    kv_items: list[dict] = [{"key": "Website", "value": url}]
    if loc:
        kv_items.append({"key": "Location", "value": loc})
    if business.get("pricing"):
        kv_items.append({"key": "Pricing", "value": str(business["pricing"])[:100]})

    blocks: list[dict] = []

    # 1. Screenshot
    if screenshot_url:
        blocks.append({"id": "panel_image", "type": "image", "url": screenshot_url})

    # 2. Assets (logos + creatives)
    logo_urls = list(business.get("logo_urls") or [])
    if not logo_urls and business.get("logo_url"):
        logo_urls = [business["logo_url"]]
    logo_displays = list(business.get("logo_displays") or [])
    if not logo_displays and business.get("logo_display"):
        logo_displays = [business["logo_display"]]

    creative_image_urls = list(business.get("creative_images") or [])
    creative_displays = list(business.get("creative_displays") or [])

    if logo_urls or creative_image_urls:
        from app.agents.adzump.agents.product.tools.scrape.receipts import (
            _asset_label,
            _build_thumbnail_row,
        )
        asset_summary = _asset_label(len(logo_urls), len(creative_image_urls))
        thumbnail_row = (
            _build_thumbnail_row(logo_urls, logo_displays)
            + _build_thumbnail_row(creative_image_urls, creative_displays)
        )
        blocks.append({"id": "assets_label", "type": "text", "content": asset_summary})
        blocks.append({"id": "assets_row", "type": "row", "children": thumbnail_row})
        blocks.append({"id": "assets_divider", "type": "divider"})

    # 3. Badge + key-values
    if business.get("business_type"):
        blocks.append({"type": "badge", "label": business["business_type"]})
    if kv_items:
        blocks.append({"type": "key_value", "items": kv_items})

    # 4. Targeting map — only after platform is known
    from app.agents.adzump.platform import is_google as _is_google, is_meta as _is_meta
    if _is_google(platform):
        target_areas = business.get("google_mapped_locations") or []
    elif _is_meta(platform):
        target_areas = business.get("meta_mapped_locations") or []
    else:
        target_areas = business.get("target_areas") or []
    business["target_areas"] = target_areas

    coords = business.get("product_coordinates") or {}
    lat = coords.get("lat")
    lng = coords.get("lng")
    if platform and ((lat is not None and lng is not None) or target_areas):
        from app.config import settings
        from app.agents.adzump.agents.location.models import is_local_business
        scale = business.get("business_scale", "local")
        blocks.append({"type": "divider"})
        blocks.append({"type": "heading", "text": "Targeting Locations"})
        blocks.append({
            "type": "map",
            "api_key": settings.GOOGLE_MAPS_API_KEY,
            "map_id": settings.GOOGLE_MAP_ID,
            "center": {"lat": lat, "lng": lng} if (lat is not None and lng is not None) else None,
            "target_areas": target_areas,
            "business_scale": is_local_business(scale) if scale else False,
            "product_location": loc,
            "platform": platform,
        })

    # 5. Product summary
    if baked_summary:
        blocks.append({"id": "summary_divider", "type": "divider"})
        blocks.append({"id": "summary_heading", "type": "heading", "text": "Product Summary"})
        blocks.append({"id": "summary_text", "type": "text", "content": baked_summary})

    # 6. Competitors
    render_competitors(blocks, competitive)

    await stream.emit_craft(
        craft_id,
        business.get("product_name") or url,
        blocks,
        append=False,
    )


async def append_competitor_blocks(
    stream,
    craft_id: str,
    business: dict,
    competitors: list[dict],
    *,
    include_headers: bool = False,
) -> None:
    """Append competitor cards to an existing craft panel without a full rebuild."""
    if not competitors:
        return
    blocks: list[dict] = []
    render_competitors(blocks, {"competitors": competitors}, include_headers=include_headers)
    if not blocks:
        return
    try:
        await stream.emit_craft(
            craft_id,
            business.get("product_name") or "",
            blocks,
            append=True,
        )
    except Exception as e:
        logger.warning("append_competitor_blocks failed: %s", e)
