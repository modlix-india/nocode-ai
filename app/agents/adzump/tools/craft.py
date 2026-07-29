"""Craft panel builder - assembles and emits the campaign side-panel.

Single responsibility: turn product_data + competitive + geo-target state
into the ordered list of craft blocks and push them to the event stream.

Imported by product.py, geo/agent.py, and competitor.py - none of those
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

    # Comparison table - every rival on one scannable surface. Ad counts aren't
    # known at analysis time (creatives are fetched separately), so the 4th column
    # is the strategic Gap rather than an ad count.
    blocks.append({
        "type": "table",
        "headers": ["Rival", "Format", "Pricing", "Gap"],
        "rows": [
            [
                c.get("name") or "?",
                str(c.get("business_type") or "-"),
                str(c.get("pricing") or "-"),
                str(c.get("weakness") or "-"),
            ]
            for c in valid
        ],
    })

    # Per-rival detail tucked behind a collapsible so the table stays uncluttered:
    # Location / USPs / why-a-competitor / website (Gap already lives in the table).
    for c in valid:
        detail: list[dict] = []
        if c.get("location"):
            detail.append({"key": "Location", "value": str(c["location"])})
        key_usps = c.get("key_usps") or []
        if isinstance(key_usps, list) and key_usps:
            detail.append(
                {"key": "USPs", "value": ", ".join(str(u) for u in key_usps[:3])}
            )
        if c.get("why_competitor"):
            detail.append({"key": "Why", "value": str(c["why_competitor"])})
        if c.get("url"):
            detail.append({"key": "Website", "value": str(c["url"])})
        if not detail:
            continue
        blocks.append({
            "type": "collapsible",
            "summary": c.get("name") or "?",
            "children": [{"type": "key_value", "items": detail}],
        })


# How many creative thumbnails to show per competitor in the panel.
_RENDER_PER_COMPETITOR = 6


def render_competitor_creatives(
    blocks: list[dict],
    competitor_name: str,
    creatives: list[dict],
    total,
    active,
) -> None:
    """Append one competitor's creative section: heading + metric tiles
    (Total/Active/Paused) + a 2-up image grid.

    Shared by the on-demand fetch (`creatives._render_creatives`) and the
    full-panel rebuild (`emit_craft_panel`) so a rebuild never drops the
    creatives. Videos tile via their poster still (flagged ▶); creatives with no
    usable image are skipped. No-op when nothing is renderable.
    """
    cards: list[dict] = []
    for c in (creatives or []):
        is_video = c.get("mediaType") == "video"
        # Prefer rehosted URLs (fileUrl/posterUrl) over the vendor's TTL-flaky
        # source URLs, whatever the media type.
        url = (c.get("posterUrl") or c.get("posterSourceUrl")) if is_video \
            else (c.get("fileUrl") or c.get("posterUrl") or c.get("sourceAssetUrl"))
        if not url:
            continue
        caption = str(c.get("headline") or "").strip()
        if is_video:
            caption = f"▶ {caption}".strip()
        card: dict = {"type": "image", "url": url}
        if caption:
            card["caption"] = caption[:120]
        cards.append(card)
        if len(cards) >= _RENDER_PER_COMPETITOR:
            break
    if not cards:
        return

    t, a = int(total or 0), int(active or 0)
    metric_row: list[dict] = [
        {"type": "metric", "label": "Total ads", "value": str(t)},
        {"type": "metric", "label": "Active", "value": str(a)},
    ]
    if t:
        metric_row.append({"type": "metric", "label": "Paused", "value": str(max(t - a, 0))})

    blocks.append({"type": "divider"})
    blocks.append({"type": "heading", "text": f"{competitor_name} - Ad Creatives", "level": 2})
    blocks.append({"type": "row", "children": metric_row})
    # 2-up grid: pairs of image blocks per row (each flexes to ~50% width).
    for i in range(0, len(cards), 2):
        blocks.append({"type": "row", "children": cards[i:i + 2]})


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

    Map section is suppressed until `platform` is set - avoids a stale
    render followed by a re-render once geo-targeting resolves.
    """
    loc = (business.get("place") or {}).get("address") or ""

    kv_items: list[dict] = [{"key": "Website", "value": url}]
    if loc:
        kv_items.append({"key": "Location", "value": loc})
    if business.get("pricing"):
        kv_items.append({"key": "Pricing", "value": str(business["pricing"])[:100]})

    blocks: list[dict] = []

    # 1. Screenshot
    if screenshot_url:
        blocks.append({"id": "panel_image", "type": "image", "url": screenshot_url})

    # 2. Assets (logos + product images)
    assets = business.get("assets") or {}
    logos = assets.get("logos") or []
    logo_urls = [l.get("url") for l in logos if l.get("url")]
    logo_displays = [l.get("display") or {} for l in logos]

    images = assets.get("images") or []
    image_urls = [i.get("url") for i in images if i.get("url")]
    image_displays = [i.get("display") or {} for i in images]

    if logo_urls or image_urls:
        from app.agents.adzump.agents.product.tools.scrape.receipts import (
            _asset_label,
            _build_thumbnail_row,
        )
        asset_summary = _asset_label(len(logo_urls), len(image_urls))
        thumbnail_row = (
            _build_thumbnail_row(logo_urls, logo_displays)
            + _build_thumbnail_row(image_urls, image_displays)
        )
        blocks.append({"id": "assets_label", "type": "text", "content": asset_summary})
        blocks.append({"id": "assets_row", "type": "row", "children": thumbnail_row})
        blocks.append({"id": "assets_divider", "type": "divider"})

    # 3. Badge + key-values
    if business.get("business_type"):
        blocks.append({"type": "badge", "label": business["business_type"]})
    if kv_items:
        blocks.append({"type": "key_value", "items": kv_items})

    # 4. Targeting map - only after platform is known
    target_areas = business.get("target_areas") or []

    place = business.get("place") or {}
    lat = place.get("lat")
    lng = place.get("lng")
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

    # 7. Competitor creatives - render them HERE, as part of the rebuild, so a
    # later rebuild never wipes the on-demand-appended grid (the disappearing-
    # creatives bug). Each competitor carries its own `creatives` + creative counts
    # once `fetch_competitor_creatives` has run; absent until then.
    for c in (competitive.get("competitors") or []):
        creatives = c.get("creatives") or []
        if not creatives:
            continue
        render_competitor_creatives(
            blocks,
            c.get("name") or "?",
            creatives,
            c.get("totalCreatives", 0),
            c.get("activeCreatives", 0),
        )

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
