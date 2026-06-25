"""Tool: fetch competitor ad creatives from the shared library.

Operates on the competitors already discovered by ``analyze_competitors`` (in
``session_context['competitor_analysis']``). For each, it consults the shared
SYSTEM creative library — serving fresh records as-is and fetching from
adlibrary.com only on a miss or stale entry (see ``services.competitor_creatives``).
Discovered creatives are attached back onto each competitor entry and rendered
into the competitor craft panel.

Intended for the **creative-inspiration phase** — invoked on explicit user
intent ("show me competitor ads") and, later, by the creative agent so a user
can pick a competitor creative and ask for a similar one. It is deliberately NOT
run during routine competitor analysis, so we don't spend ad-library credits
unless creatives are actually wanted.
"""

from __future__ import annotations

import logging

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump._shared import emit_progress
from app.agents.adzump.services import creative_library as lib
from app.agents.adzump.services import competitor_creatives as cc

logger = logging.getLogger(__name__)

# How many creative thumbnails to show per competitor in the craft panel.
_RENDER_PER_COMPETITOR = 6


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("true", "1", "yes")


async def _render_creatives(stream, craft_id: str, title: str, competitor_name: str,
                            record: dict) -> None:
    """Append one competitor's creatives (heading + stats + thumbnails) to the
    craft panel. Uses the rehosted ``fileUrl`` when present, else the source URL."""
    creatives = record.get("creatives") or []
    blocks: list[dict] = [
        {"type": "divider"},
        {"type": "heading", "text": f"{competitor_name} — Ad Creatives", "level": 2},
        {"type": "key_value", "items": [
            {"key": "Total ads", "value": str(record.get("totalCreatives", 0))},
            {"key": "Active", "value": str(record.get("activeCreatives", 0))},
        ]},
    ]
    shown = 0
    for c in creatives:
        is_video = c.get("mediaType") == "video"
        # An image block can't play video, so videos are tiled via their poster
        # still and flagged with ▶. Creatives with no usable image are skipped.
        url = (c.get("posterUrl") or c.get("posterSourceUrl")) if is_video \
            else (c.get("fileUrl") or c.get("sourceAssetUrl"))
        if not url:
            continue
        blocks.append({"type": "image", "url": url})
        caption = str(c.get("headline") or "").strip()
        if is_video:
            caption = f"▶ {caption}".strip()
        if caption:
            blocks.append({"type": "text", "content": caption[:120]})
        shown += 1
        if shown >= _RENDER_PER_COMPETITOR:
            break
    if shown == 0:
        return
    try:
        await stream.emit_craft(craft_id, title, blocks, append=True)
    except Exception as e:
        logger.warning("creative_craft_append_failed: %s", str(e)[:200])


async def _fetch_competitor_creatives(params: dict, context: dict) -> ToolResult:
    """Fetch + cache competitor creatives for the current competitor set."""
    session_ctx = context.get("session_context", {}) or {}
    competitive = session_ctx.get("competitor_analysis") or {}
    competitors = competitive.get("competitors") or []
    if not competitors:
        return ToolResult(
            success=False,
            error="No competitors to fetch creatives for. Run analyze_competitors first.",
        )

    force = _truthy(params.get("force"))
    await emit_progress(context, "Fetching competitor creatives…")

    try:
        results = await cc.fetch_for_competitors(competitors, context, force=force)
    except Exception as e:
        logger.warning("fetch_competitor_creatives failed: %s: %s",
                       type(e).__name__, str(e)[:200])
        return ToolResult(success=False, error=f"Creative fetch failed: {e}")

    # Attach creatives back to each competitor entry (persisted in session) and
    # render them into the existing craft panel.
    stream = context.get("event_stream")
    craft_id = session_ctx.get("craft_id", "")
    business = session_ctx.get("product_data") or {}
    title = business.get("product_name", "")

    total_creatives = 0
    enriched = 0
    for comp in competitors:
        key, name = cc.competitor_identity(comp)
        record = results.get(key)
        if not record:
            continue
        comp["creatives"] = record.get("creatives") or []
        comp["creativeStats"] = {
            "total": record.get("totalCreatives", 0),
            "active": record.get("activeCreatives", 0),
        }
        total_creatives += record.get("totalCreatives", 0)
        enriched += 1
        if stream and craft_id:
            await _render_creatives(stream, craft_id, title, name or key, record)

    summary = (
        f"Fetched creatives for {enriched} competitor"
        f"{'s' if enriched != 1 else ''} ({total_creatives} ads total)."
        if enriched else "No creatives found for the current competitors."
    )
    return ToolResult(
        success=True,
        data={"resolved": list(results.keys()), "total_creatives": total_creatives},
        summary=summary,
        audience="both",
    )


fetch_competitor_creatives = ToolDefinition(
    name="fetch_competitor_creatives",
    description=(
        "Fetch competitor ad creatives (image/video thumbnails, ad copy, metrics) "
        "to use as creative inspiration. Call ONLY when the user explicitly wants "
        "to see the ads competitors are running, or to gather reference for "
        "generating similar creatives — NOT as a routine step of competitor "
        "analysis. Requires competitors to already exist (from analyze_competitors). "
        "Reuses a shared creative library and only queries the ad library for "
        "competitors that are missing or stale. Use force='true' to ignore the "
        "cache and refetch."
    ),
    display_name="Fetch Competitor Creatives",
    parameters=[
        ToolParameter(
            name="force",
            type="string",
            description="Set to 'true' to refetch from the ad library, ignoring cached library data.",
            required=False,
        ),
    ],
    execute=_fetch_competitor_creatives,
)

CREATIVE_TOOLS = [fetch_competitor_creatives]
