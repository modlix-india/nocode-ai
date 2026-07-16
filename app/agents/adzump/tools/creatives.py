"""Tool: fetch competitor ad creatives from the Creative Intelligence library.

Operates on the competitors already discovered by ``analyze_competitors`` (in
``session_context['competitor_analysis']``). For each, it reads the shared
library - serving fresh records as-is and fetching from the source only on a miss
or stale entry (see ``creative_intelligence.library``). Discovered creatives are
attached back onto each competitor entry and rendered into the craft panel.

Intended for the creative-inspiration phase - the model calls it when the user
wants to see competitor ads, and later the creative agent will call it to gather
reference. It is deliberately NOT run during routine competitor analysis, so we
don't spend ad-library credits unless creatives are actually wanted.
"""

from __future__ import annotations

import logging

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump._shared import emit_progress
from app.agents.adzump import creative_intelligence as ci
from app.agents.adzump.tools.craft import render_competitor_creatives

logger = logging.getLogger(__name__)


async def _render_creatives(stream, craft_id: str, title: str, competitor_name: str,
                            creatives: list[dict], total: int, active: int) -> None:
    """Append one competitor's creative section (heading + metric tiles + 2-up
    grid) to the craft panel via the shared builder - the SAME one the full-panel
    rebuild uses, so the appended grid and the rebuilt grid are byte-identical."""
    blocks: list[dict] = []
    render_competitor_creatives(blocks, competitor_name, creatives, total, active)
    if not blocks:
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

    force = bool(params.get("force"))
    await emit_progress(context, "Fetching competitor creatives…")

    try:
        results = await ci.creatives_for_all(competitors, context, force=force)
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
        key, name = ci.competitor_identity(comp)
        record = results.get(key)
        if not record:
            continue
        dumped = record.model_dump(by_alias=True)
        comp["creatives"] = dumped["creatives"]
        comp["totalCreatives"] = dumped["totalCreatives"]
        comp["activeCreatives"] = dumped["activeCreatives"]
        total_creatives += dumped["totalCreatives"]
        enriched += 1
        if stream and craft_id:
            await _render_creatives(
                stream, craft_id, title, name or key,
                dumped["creatives"], dumped["totalCreatives"], dumped["activeCreatives"],
            )

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
        "generating similar creatives - NOT as a routine step of competitor "
        "analysis. Requires competitors to already exist (from analyze_competitors). "
        "Reuses a shared creative library and only queries the ad library for "
        "competitors that are missing or stale. Set force=true to ignore the "
        "cache and refetch."
    ),
    display_name="Fetch Competitor Creatives",
    parameters=[
        ToolParameter(
            name="force",
            type="boolean",
            description="Set true to refetch from the ad library, ignoring cached library data.",
            required=False,
        ),
    ],
    execute=_fetch_competitor_creatives,
)

CREATIVE_TOOLS = [fetch_competitor_creatives]
