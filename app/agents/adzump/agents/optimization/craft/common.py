"""Common craft utilities — fallback views and multi-campaign dashboard.

Uses ``PlatformCraftRegistry`` to dynamically dispatch block rendering
per campaign, eliminating hardcoded platform branches.
"""

from __future__ import annotations

import logging

from app.agents.adzump.agents.optimization.craft.base import PlatformCraftRegistry
from app.agents.adzump.agents.optimization.craft.builders import (
    accordion_block,
    badge_block,
    callout_block,
    heading_block,
    tabs_block,
)
from app.agents.adzump.agents.optimization.models import CampaignRecommendation
from app.core.streaming import AgentEventStream

logger = logging.getLogger(__name__)


async def emit_fallback_craft(
    event_stream: AgentEventStream,
    craft_id: str,
    title: str,
) -> None:
    """Emit a clean empty state callout to the Craft Panel."""
    blocks = [
        callout_block(
            "No active campaign recommendations found. "
            "Running a nightly scheduled runner or triggering a live single-campaign scan "
            "will populate this dashboard with diagnostics, bidding strategies, "
            "and targeting action plans.",
            variant="info",
        )
    ]
    await event_stream.emit_craft(craft_id, title, blocks)


def _build_campaign_blocks(rec: CampaignRecommendation) -> list[dict]:
    """Build compact blocks for a single campaign using its platform renderer.

    Delegates to the registered renderer's ``render_compact_blocks`` method.
    Catches all exceptions to prevent a single broken campaign from crashing
    the entire multi-campaign dashboard.
    """
    try:
        renderer = PlatformCraftRegistry.get_renderer(rec.platform)
        return renderer.render_compact_blocks(rec)
    except Exception as e:
        logger.error(
            "Failed to build campaign blocks for %s: %s",
            rec.campaign_id, e, exc_info=True,
        )
        return [callout_block("Unable to display recommendations for this campaign.", "danger")]


async def emit_multi_campaign_craft(
    event_stream: AgentEventStream,
    craft_id: str,
    recommendations: list[CampaignRecommendation],
    title: str,
) -> None:
    """Emit a multi-campaign dashboard with accordions grouped by platform tabs."""
    blocks: list[dict] = [
        heading_block("Account Optimization Summary", level=1),
        badge_block(f"Active Campaigns: {len(recommendations)}"),
    ]

    # Group by platform
    by_platform: dict[str, list[CampaignRecommendation]] = {}
    PLATFORM_DISPLAY_NAMES = {
        "GOOGLE": "Google Ads",
        "META": "Meta Ads",
    }
    PLATFORM_SHORT_NAMES = {
        "GOOGLE": "Google",
        "META": "Meta",
    }

    for rec in recommendations:
        plat = (rec.platform or "GOOGLE").upper()
        platform_name = PLATFORM_DISPLAY_NAMES.get(plat, f"{plat.title()} Ads")
        by_platform.setdefault(platform_name, []).append(rec)

    if len(by_platform) > 1:
        # Multiple platforms → use tabs
        tabs_items = []
        for platform_name, recs in by_platform.items():
            platform_blocks = []
            for rec in recs:
                campaign_blocks = _build_campaign_blocks(rec)
                plat_key = (rec.platform or "GOOGLE").upper()
                short_plat = PLATFORM_SHORT_NAMES.get(plat_key, plat_key.title())
                platform_blocks.append(
                    accordion_block(
                        f"{rec.campaign_name} ({short_plat})",
                        campaign_blocks,
                        is_open=len(recs) == 1,
                    )
                )
            tabs_items.append({
                "label": platform_name,
                "blocks": platform_blocks,
            })
        blocks.append(tabs_block(tabs_items))
    else:
        # Single platform → list accordions directly
        for rec in recommendations:
            campaign_blocks = _build_campaign_blocks(rec)
            plat_key = (rec.platform or "GOOGLE").upper()
            short_plat = PLATFORM_SHORT_NAMES.get(plat_key, plat_key.title())
            blocks.append(
                accordion_block(
                    f"{rec.campaign_name} ({short_plat})",
                    campaign_blocks,
                    is_open=len(recommendations) == 1,
                )
            )

    logger.info(
        "emit_multi_campaign_craft: emitting %d blocks for %d campaigns craft_id=%s",
        len(blocks), len(recommendations), craft_id,
    )
    await event_stream.emit_craft(craft_id, title, blocks)
