"""prepare_campaign_review — spawn the CampaignAgent after the user confirms the summary.

Called by the main agent once the campaign details are collected and confirmed. Reads
campaign_spec + product_data from the session, spawns the CampaignAgent (which runs the
platform's creation tools — keyword research for Search, audience targeting for Demand
Gen), and persists the build it produced back onto the session for review and launch.
"""

from __future__ import annotations

import logging

from app.agents.adzump.agents.campaign.agent import get_campaign_agent
from app.agents.adzump.agents.campaign.models import (
    build_dump,
    build_missing,
    is_build_complete,
    set_build,
)
from app.agents.adzump.platform import is_google as platform_is_google
from app.core.streaming import pre_emit_agent_started
from app.core.tools.base import ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

# A failed build leaves the slot empty, so _next_action re-prescribes this tool and the model
# calls it again. Per TURN, not per session - the session outlives the loop.
_MAX_BUILD_ATTEMPTS = 2
_ATTEMPTS_KEY = "_build_attempts"


async def _prepare_campaign_review(params: dict, context: dict) -> ToolResult:
    session_ctx = context.get("session_context")
    if session_ctx is None:
        return ToolResult(success=False, error="No session context available.")

    turn = getattr(context.get("_session"), "_turn_count", 0) or 0
    tally = session_ctx.get(_ATTEMPTS_KEY) or {}
    attempts = tally.get("count", 0) if tally.get("turn") == turn else 0
    if attempts >= _MAX_BUILD_ATTEMPTS:
        logger.warning("prepare_campaign_review: giving up after %d attempts", attempts)
        return ToolResult(
            success=False,
            error=(
                f"The campaign build has already failed {attempts} times this turn. Do NOT "
                "call this tool again. Tell the user it could not be built, and stop."
            ),
        )

    spec = session_ctx.get("campaign_spec") or {}
    if not spec.get("account"):
        return ToolResult(
            success=False,
            error="Campaign details incomplete — no ad account selected yet.",
        )

    # The channel decides WHICH build runs, and resolve_channel falls back to Search - so
    # skipping the ask silently builds a Search campaign instead of failing.
    if platform_is_google(spec.get("platform")) and not spec.get("channel"):
        logger.warning("prepare_campaign_review: no channel chosen yet")
        return ToolResult(
            success=False,
            error=(
                "Cannot build yet — the user has not chosen a Google campaign type. Ask "
                'with present_options(field="channel") first, then call this again.'
            ),
        )
    # set_build below replaces the whole build, and the model reaches for this tool whenever
    # it wants progress - so a stray call on an edit request discards the user's review. A
    # PARTIAL build still retries: create() now carries it in, so the gap is filled rather
    # than rebuilt.
    if is_build_complete(session_ctx):
        logger.warning("prepare_campaign_review: refused - build already complete")
        return ToolResult(
            success=False,
            error=(
                "This campaign is ALREADY built and the user is reviewing it. Building "
                "again would discard every edit they made. Never call this tool twice for "
                "one campaign. If they asked to change the audience call manage_audience "
                "with their verbatim message; for keywords call manage_keywords; if they "
                "are done, ask them to launch."
            ),
        )

    auth = context.get("auth")
    if auth is None:
        return ToolResult(success=False, error="No auth context for campaign creation.")
    stream = context.get("event_stream")
    if stream is None:
        return ToolResult(success=False, error="No event stream for campaign creation.")
    # Always a separate card from the product craft (adzump_{session_id}).
    craft_id = f"campaign_{context.get('session_id') or ''}"
    session_ctx["campaign_craft_id"] = craft_id

    # Launcher owns the agent-card span: pre-emit started here (bound to this tool call);
    # the CampaignAgent emits agent_finished when it's done.
    await pre_emit_agent_started(
        stream,
        agent_id="campaign",
        label="Campaign Creation",
        parent_tool_use_id=context.get("tool_use_id", ""),
        context=context,
    )
    session_ctx[_ATTEMPTS_KEY] = {"turn": turn, "count": attempts + 1}
    result = await get_campaign_agent().create(
        campaign_spec=spec,
        product_data=session_ctx.get("product_data") or {},
        craft_id=craft_id,
        parent_event_stream=stream,
        auth=auth,
        build=build_dump(session_ctx),
    )
    if not result:
        return ToolResult(
            success=False,
            error="Campaign creation produced nothing to review — check the ad account and retry.",
        )
    set_build(session_ctx, result)
    # A partial build is a failed one. channel_controls writes its slot unconditionally, so
    # "not empty" would pass a run whose audience died and hand the user an empty panel -
    # and clearing the attempt counter would let it retry forever.
    if missing := build_missing(session_ctx):
        logger.warning("campaign_build_incomplete: missing=%s", ", ".join(missing))
        return ToolResult(
            success=False,
            error=(
                f"Campaign setup is missing {', '.join(missing)} — that step did not "
                "complete. Tell the user which part failed and ask before retrying."
            ),
        )
    session_ctx.pop(_ATTEMPTS_KEY, None)

    # The elicitation owns its ask (the deferred break skips the follow-up turn).
    await stream.emit_text(
        "\n\nYour campaign setup is ready in the panel — review it, make any changes, "
        "then tell me to launch when you're ready after reviewing.\n"
    )

    return ToolResult(
        success=True,
        summary="Campaign setup complete; the review panel and prompt are on screen.",
        # Multi-message review elicitation: resume on the next real message, not per edit.
        data={"craft_id": craft_id, "elicited": True, "elicit_expects": "multi"},
    )


prepare_campaign_review = ToolDefinition(
    name="prepare_campaign_review",
    description=(
        "Prepare the confirmed campaign for review — runs the selected platform's campaign "
        "build and opens the review panel for edits before launch. Call once, after the user "
        "confirms the campaign summary. Takes no parameters."
    ),
    display_name="Prepare Campaign Review",
    parameters=[],
    execute=_prepare_campaign_review,
)

PREPARE_CAMPAIGN_REVIEW_TOOLS = [prepare_campaign_review]
