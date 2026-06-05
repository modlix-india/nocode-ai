"""Craft-panel receipts for the ProductAgent's asset selection.

Replaces the `assets_label` and `assets_row` placeholders seeded by
`scrape_profile._generate_business_profile`. The layout emit MUST run
before the first receipt emit, or id-based merge targets non-existent
blocks and receipts silently drop. Ordering is enforced by `_scrape_url`
in scrape.py.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def _emit_asset_receipts(
    stream, craft_id: str, url: str, product_data: dict
) -> None:
    """Replace the assets_label + assets_row placeholders in the craft panel.

    Each call grows the receipts row in place via the UI's id-based merge.
    """
    if not stream or not craft_id:
        return
    logo_urls = list(product_data.get("logo_urls") or [])
    if not logo_urls and product_data.get("logo_url"):
        logo_urls = [product_data["logo_url"]]
    logo_displays = list(product_data.get("logo_displays") or [])
    if not logo_displays and product_data.get("logo_display"):
        logo_displays = [product_data["logo_display"]]
    creative_image_urls = list(product_data.get("creative_images") or [])
    creative_displays = list(product_data.get("creative_displays") or [])
    if not logo_urls and not creative_image_urls:
        return

    summary = _asset_label(len(logo_urls), len(creative_image_urls))
    thumbnail_row = (
        _build_thumbnail_row(logo_urls, logo_displays)
        + _build_thumbnail_row(creative_image_urls, creative_displays)
    )
    blocks = [
        {"id": "assets_label", "type": "text", "content": summary},
        {"id": "assets_row", "type": "row", "children": thumbnail_row},
    ]
    try:
        await stream.emit_craft(craft_id, url, blocks, append=True)
        logger.info(
            "assets_stage:emitted craft_id=%s label=%r tiles=%d",
            craft_id, summary, len(thumbnail_row),
        )
    except Exception as e:
        logger.warning("assets_stage:emit_failed err=%s", str(e)[:120])


def _asset_label(logo_count: int, creative_count: int) -> str:
    parts: list[str] = []
    if logo_count:
        parts.append("Logo" if logo_count == 1 else f"{logo_count} logos")
    if creative_count:
        parts.append(f"{creative_count} product image" + ("" if creative_count == 1 else "s"))
    return " · ".join(parts)


def _build_thumbnail_row(urls: list[str], displays: list[dict]) -> list[dict]:
    """Each tile renders the 256px thumbnail inline but links to the
    full-resolution URL on click. The image block carries both so the UI
    can use thumb_url for <img src> and url for the click target."""
    row: list[dict] = []
    for idx, src in enumerate(urls):
        display = displays[idx] if idx < len(displays) else {}
        thumb_url = display.get("thumb_url")
        block: dict = {
            "type": "image",
            "url": src,
            "size": "thumbnail",
        }
        if thumb_url:
            block["thumb_url"] = thumb_url
        for k, v in display.items():
            if v and k not in ("thumb_url", "url"):
                block[k] = v
        row.append(block)
    return row
