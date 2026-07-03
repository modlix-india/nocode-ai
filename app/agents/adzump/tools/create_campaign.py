"""create_campaign — spawn the CampaignAgent after the user confirms the summary.

Called by the main agent once the campaign details are collected and confirmed. Reads
campaign_spec + product_data from the session, spawns the CampaignAgent (which runs the
platform's creation tools — for Google Search, keyword research), and persists the
keyword_research result back onto the session for review and launch.
"""

from __future__ import annotations

import logging

from app.core.streaming import pre_emit_agent_started
from app.core.tools.base import ToolDefinition, ToolResult

from app.agents.adzump.agents.campaign.agent import get_campaign_agent

logger = logging.getLogger(__name__)


async def _create_campaign(params: dict, context: dict) -> ToolResult:
    session_ctx = context.get("session_context")
    if session_ctx is None:
        return ToolResult(success=False, error="No session context available.")
    spec = session_ctx.get("campaign_spec") or {}
    if not spec.get("account"):
        return ToolResult(success=False, error="Campaign details incomplete — no ad account selected yet.")
    auth = context.get("auth")
    if auth is None:
        return ToolResult(success=False, error="No auth context for campaign creation.")

    stream = context.get("event_stream")
    # Always a separate card from the product craft (adzump_{session_id}).
    craft_id = f"campaign_{context.get('session_id') or ''}"
    session_ctx["campaign_craft_id"] = craft_id

    # Launcher owns the agent-card span: pre-emit started here (bound to this tool call);
    # the CampaignAgent emits agent_finished when it's done.
    await pre_emit_agent_started(
        stream, agent_id="campaign", label="Campaign Creation",
        parent_tool_use_id=context.get("tool_use_id", ""), context=context,
    )
    result = await get_campaign_agent().create(
        campaign_spec=spec,
        product_data=session_ctx.get("product_data") or {},
        craft_id=craft_id,
        parent_event_stream=stream,
        auth=auth,
    )
    if not result:
        return ToolResult(
            success=False,
            error="Campaign creation produced no keyword results — check the ad account and retry.",
        )
    session_ctx["keyword_research"] = result

    # The elicitation owns its ask (the deferred break skips the follow-up turn).
    if stream:
        await stream.emit_text(
            "\n\nYour campaign setup is ready in the panel — review it, make any changes, "
            "then tell me to launch when you're happy.\n"
        )

    return ToolResult(
        success=True,
        summary="Campaign setup complete; the review panel and prompt are on screen.",
        # Multi-message review elicitation: resume on the next real message, not per edit.
        data={"craft_id": craft_id, "elicited": True, "elicit_expects": "multi"},
    )


create_campaign = ToolDefinition(
    name="create_campaign",
    description=(
        "Build the campaign for the confirmed details — researches brand + generic keywords "
        "(Google Search) and shows them in the review panel. Call once, after the user confirms "
        "the campaign summary and before launch. Takes no parameters."
    ),
    display_name="Create Campaign",
    parameters=[],
    execute=_create_campaign,
)

CREATE_CAMPAIGN_TOOLS = [create_campaign]
