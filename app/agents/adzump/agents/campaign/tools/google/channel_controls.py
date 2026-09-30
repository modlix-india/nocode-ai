"""channel_controls — which surfaces a Demand Gen ad may show on.

Build tool and panel mutation in one module: the change is a single boolean, so splitting it
the way the audience mutation is split would be ceremony.

The default is everything the ad type can serve on; narrowing is the user's, from the panel.
"""

from __future__ import annotations

import logging

from app.agents.adzump.agents.campaign.craft import (
    channel_controls_block,
    emit_section_update,
)
from app.agents.adzump.agents.campaign.google import channel_controls as controls
from app.agents.adzump.agents.campaign.models import (
    Channel,
    resolve_channel,
    set_channel_controls,
)
from app.agents.adzump.agents.campaign.models import (
    channel_controls as saved_controls,
)
from app.agents.adzump.agents.campaign.models import (
    creative as saved_creative,
)
from app.core.tools.base import ToolDefinition, ToolResult

logger = logging.getLogger(__name__)


async def _emit(context: dict, session_ctx: dict) -> None:
    craft_id = (
        session_ctx.get("campaign_craft_id")
        or session_ctx.get("craft_id")
        or f"campaign_{context.get('session_id', '')}"
    )
    await emit_section_update(
        context.get("event_stream"),
        craft_id,
        channel_controls_block(
            saved_controls(session_ctx),
            controls.ad_type_for(saved_creative(session_ctx)),
        ),
    )


async def _channel_controls(params: dict, context: dict) -> ToolResult:
    session_ctx = context.get("session_context")
    if session_ctx is None:
        return ToolResult(success=False, error="No session context available.")

    channel = resolve_channel(session_ctx.get("campaign_spec") or {})
    if channel is not Channel.DEMAND_GEN:
        return ToolResult(
            success=True,
            summary=f"{channel.value} campaigns do not choose surfaces — skipped.",
            data={"skipped": True},
        )

    ad_type = controls.ad_type_for(saved_creative(session_ctx))
    set_channel_controls(session_ctx, controls.defaults(ad_type))
    await _emit(context, session_ctx)
    on = [s.label for s in controls.SURFACES if ad_type in s.serves]
    return ToolResult(
        success=True,
        summary=f"Ads will show on {', '.join(on)}.",
        data={"surfaces": len(on)},
    )


async def update_channel_controls(params: dict, context: dict) -> ToolResult:
    """Panel-click entry: one surface on or off, zero LLM."""
    session_ctx = context.get("session_context")
    if session_ctx is None:
        return ToolResult(success=False, error="No session context available.")

    # Guard the write, not just the build: a widget message can arrive on any campaign, and
    # set_channel_controls would replace a Search build wholesale - destroying its keywords.
    if (
        resolve_channel(session_ctx.get("campaign_spec") or {})
        is not Channel.DEMAND_GEN
    ):
        return ToolResult(
            success=False, error="This campaign does not choose where ads show."
        )

    surface = str(params.get("surface") or "").strip()
    enabled = bool(params.get("enabled"))
    updated, error = controls.toggle(
        saved_controls(session_ctx),
        surface,
        enabled,
        controls.ad_type_for(saved_creative(session_ctx)),
    )
    if updated is None:
        return ToolResult(success=False, error=error)

    set_channel_controls(session_ctx, updated)
    await _emit(context, session_ctx)
    label = next((s.label for s in controls.SURFACES if s.key == surface), surface)
    logger.info("channel_controls %s=%s", surface, enabled)
    return ToolResult(
        success=True,
        summary=f"{label} turned {'on' if enabled else 'off'}.",
        data={"surface": surface, "enabled": enabled},
    )


channel_controls = ToolDefinition(
    name="channel_controls",
    description=(
        "Choose where a Demand Gen campaign's ads can show — YouTube, Discover, Gmail and "
        "the Display Network — and show them in the review panel. Call once, after the "
        "audience."
    ),
    display_name="Where Ads Show",
    parameters=[],
    execute=_channel_controls,
)
