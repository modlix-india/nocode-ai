"""Craft package — campaign recommendation visual rendering.

Entry point for emitting Craft blocks to the frontend panel.
Uses ``PlatformCraftRegistry`` for dynamic platform dispatch.

Architecture:
  - ``base.py``      → ABC + Registry
  - ``builders.py``  → Type-safe block constructors
  - ``google.py``    → GoogleCraftRenderer (registered at import)
  - ``meta.py``      → MetaCraftRenderer (registered at import)
  - ``common.py``    → Multi-campaign dashboard + fallback views

Adding a new platform:
  1. Create ``newplatform.py`` with a class decorated
     ``@PlatformCraftRegistry.register("NEWPLATFORM")``
  2. Import it below so the decorator executes at load time.
"""

from __future__ import annotations

import logging

from app.agents.adzump.agents.optimization.models import CampaignRecommendation
from app.core.streaming import AgentEventStream

from .base import PlatformCraftRegistry
from .builders import callout_block
from .common import emit_fallback_craft, emit_multi_campaign_craft

# Force-import platform modules to trigger @register decorators.
# Without these imports, the registry will be empty at runtime.
from . import google  # noqa: F401
from . import meta  # noqa: F401

logger = logging.getLogger(__name__)


async def emit_campaign_recommendations_craft(
    event_stream: AgentEventStream,
    craft_id: str,
    recommendations: CampaignRecommendation | list[CampaignRecommendation] | None,
    title: str,
) -> None:
    """Main routing dispatcher to emit campaign dashboards to the Craft Panel.

    - ``None`` or empty list  →  fallback empty-state callout
    - Single recommendation   →  full detail view via platform renderer
    - Multiple recommendations →  tabbed/accordion multi-campaign view

    All rendering errors are caught and surfaced as a "danger" callout
    in the Craft Panel, so the user never sees a blank panel.
    """
    if not recommendations:
        await emit_fallback_craft(event_stream, craft_id, title)
        return

    if isinstance(recommendations, list):
        if len(recommendations) == 1:
            recommendations = recommendations[0]
        else:
            logger.info(
                "emit_craft: dispatching multi-campaign view count=%d craft_id=%s",
                len(recommendations), craft_id,
            )
            await emit_multi_campaign_craft(
                event_stream, craft_id, recommendations, title
            )
            return

    # Single recommendation — full detail render
    platform = (recommendations.platform or "GOOGLE").upper()
    try:
        renderer = PlatformCraftRegistry.get_renderer(platform)
        blocks = renderer.render_recommendation_blocks(recommendations)
        await event_stream.emit_craft(craft_id, title, blocks)
    except Exception as e:
        logger.error(
            "emit_campaign_recommendations_craft failed platform=%s campaign=%s",
            platform,
            recommendations.campaign_id,
            exc_info=True,
        )
        # Yield a clean error card so the user sees a premium warning
        # instead of a blank panel
        blocks = [
            callout_block(
                f"I encountered an error compiling the dashboard visuals: {e}. "
                "Stored optimization data is intact.",
                variant="danger",
            )
        ]
        await event_stream.emit_craft(craft_id, title, blocks)
