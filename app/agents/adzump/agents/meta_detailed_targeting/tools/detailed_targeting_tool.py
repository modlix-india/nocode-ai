"""Detailed targeting tool - launches DetailedTargetingAgent sub-agent to generate
Meta Ads detailed targeting suggestions (interests, demographics, behaviors).

UI-triggered mutations (keyword search, add segment from search, delete chip) are
handled by REST endpoints in targeting_router.py and bypass the LLM entirely.

This module contains only:
  - suggest_meta_targeting  : spawns the sub-agent for strategic recommendations.
  - delete_targeting_segment: LLM-callable tool for conversational segment deletes
                              (e.g. "remove the Real Estate segment"). UI chip
                              deletes use the REST DELETE endpoint instead.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.core.streaming import pre_emit_agent_started

from app.agents.adzump.agents.meta_detailed_targeting.agent import (
    get_detailed_targeting_agent,
)
from app.agents.adzump.agents.meta_detailed_targeting.models import (
    MetaTargetingSuggestionResult,
    TargetingEntity,
)

logger = logging.getLogger(__name__)


def _build_suggestion_result(
    targeting: dict[str, Any],
) -> MetaTargetingSuggestionResult:
    """Helper to parse raw segment lists into a MetaTargetingSuggestionResult."""
    all_raw = (
        targeting.get("interests", [])
        + targeting.get("demographics", [])
        + targeting.get("behaviors", [])
        + targeting.get("entities", [])
    )

    entities = []
    for item in all_raw:
        if isinstance(item, TargetingEntity):
            entities.append(item)
        elif isinstance(item, dict):
            parsed = TargetingEntity.from_meta(item)
            if parsed is not None:
                entities.append(parsed)

    return MetaTargetingSuggestionResult(entities=entities)


# ---------------------------------------------------------------------------
# suggest_meta_targeting — spawns the AI sub-agent
# ---------------------------------------------------------------------------

async def _suggest_meta_targeting(
    params: dict[str, Any], context: dict[str, Any]
) -> ToolResult:
    """Spawn the Detailed Targeting sub-agent to discover and validate segments.

    Pre-emits agent started, calls agent recommendation, stashes result, and returns ToolResult.
    """
    stream = context.get("event_stream")
    tool_use_id = context.get("tool_use_id", "")
    auth = context.get("auth")
    session_ctx = context.get("session_context", {}) or {}
    parent_session = context.get("_session")

    # 1. Resolve ad account ID
    ad_account_id = (params.get("ad_account_id") or "").strip()
    if not ad_account_id:
        spec = session_ctx.get("campaign_spec") or {}
        ad_account_id = (spec.get("account") or "").strip()

    if not ad_account_id:
        return ToolResult(
            success=False,
            error="ad_account_id is required. Please select or provide a Meta ad account first.",
        )

    # 2. Resolve URL
    url = (
        (session_ctx.get("product_profile") or {}).get("url")
        or (session_ctx.get("product_data") or {}).get("url")
        or ""
    )
    if not url:
        return ToolResult(
            success=False,
            error="No website URL found. Please perform website analysis first.",
        )

    if auth is None:
        return ToolResult(
            success=False,
            error="No auth context available. Authentication is required to run Meta API queries.",
        )

    if parent_session is None:
        return ToolResult(
            success=False,
            error="No active session found. Session context is required.",
        )

    # Start the sub-agent card span in the UI
    await pre_emit_agent_started(
        stream,
        agent_id="detailed_targeting",
        label="Targeting Analyst",
        parent_tool_use_id=tool_use_id,
        context=context,
    )

    try:
        user_query = (params.get("user_query") or "").strip()

        # Run sub-agent detailed targeting suggestion pipeline
        result, explanation = await get_detailed_targeting_agent().recommend(
            session_id=parent_session.session_id,
            ad_account_id=ad_account_id,
            parent_event_stream=stream,
            auth=auth,
            parent_session_context=session_ctx,
            parent_tool_use_id=tool_use_id,
            user_query=user_query,
        )

        # Stash structured suggestions back to parent session context
        session_ctx["detailed_targeting"] = result.model_dump()

        summary = f"Suggested {len(result.entities)} detailed targeting segments."
        if explanation:
            summary += f"\n\n{explanation}"

        return ToolResult(
            success=True,
            data=result.model_dump(),
            summary=summary,
        )

    except Exception as e:
        logger.exception("Detailed targeting discovery failed")
        return ToolResult(
            success=False,
            error=f"Detailed targeting suggestions failed: {type(e).__name__}: {e}",
        )


suggest_meta_targeting = ToolDefinition(
    name="suggest_meta_targeting",
    description=(
        "Query the AI Targeting Analyst sub-agent to discover and recommend Meta detailed targeting "
        "segments (interests, behaviors, demographics) for this campaign. "
        "Use this for strategic requests such as 'generate targeting', 'expand my audience', "
        "or 'find more segments for luxury watch buyers'. "
        "Do NOT use this for keyword searches, chip deletes, or adding a specific segment — "
        "those are handled by the craft panel directly."
    ),
    display_name="Suggest Meta Targeting",
    parameters=[
        ToolParameter(
            name="ad_account_id",
            type="string",
            description=(
                "Meta ad account ID (e.g. '508128451820487' or 'act_508128451820487'). "
                "If not provided, it will fallback to the stashed account ID in the campaign spec."
            ),
            required=False,
        ),
        ToolParameter(
            name="user_query",
            type="string",
            description="The user's specific instructions or query regarding targeting. Pass this so the agent can tailor its search.",
            required=False,
        ),
    ],
    execute=_suggest_meta_targeting,
)


# ---------------------------------------------------------------------------
# delete_targeting_segment — LLM-callable for conversational deletes
#
# Use case: user says "remove the Real Estate segment" or "clear all behaviors"
# in the chat. The UI chip delete button uses the REST DELETE endpoint instead.
# ---------------------------------------------------------------------------

async def _delete_targeting_segment(
    params: dict[str, Any], context: dict[str, Any]
) -> ToolResult:
    """Delete one or more targeting segments by name, ID, or category.

    Called when the user asks conversationally to remove segments, e.g.:
      "delete the Real Estate segment"
      "remove all behavior segments"
      "clear everything"

    UI chip delete buttons use the REST DELETE endpoint (targeting_router.py)
    and never invoke this tool.
    """
    target_id = (params.get("target_id") or "").strip()
    name = (params.get("name") or "").strip()
    category = (params.get("category") or "").strip().lower()

    if not target_id and not name and not category:
        return ToolResult(
            success=False,
            error="Provide at least one of: target_id, name, or category to identify what to delete.",
        )

    session_ctx = context.get("session_context", {}) or {}
    stream = context.get("event_stream")
    session = context.get("_session")

    if not session:
        return ToolResult(success=False, error="No active session found in tool context.")

    targeting = session_ctx.setdefault("detailed_targeting", {})
    if not isinstance(targeting, dict):
        targeting = {}
        session_ctx["detailed_targeting"] = targeting

    agent = get_detailed_targeting_agent()
    parent_session_id = session_ctx.get("_parent_session_id") or session.session_id
    craft_id = f"detailed_targeting_{parent_session_id}"

    orig_list = targeting.get("entities") or []
    new_list = []
    removed_names: list[str] = []

    clear_all = name.lower() in ("all", "everything", "all segments") if name else False

    for item in orig_list:
        item_id = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
        item_name = item.get("name") if isinstance(item, dict) else getattr(item, "name", "")
        item_type = (item.get("type") if isinstance(item, dict) else getattr(item, "type", "")) or ""

        match_id = target_id and str(item_id) == str(target_id)
        match_name = name and not clear_all and name.lower() in item_name.lower()
        match_category = category and item_type.lower() == category
        match_clear_all = clear_all

        if match_id or match_name or match_category or match_clear_all:
            removed_names.append(item_name)
        else:
            new_list.append(item)

    targeting["entities"] = new_list
    result = _build_suggestion_result(targeting)
    session_ctx["detailed_targeting"] = result.model_dump()

    search_results = session_ctx.get("detailed_targeting_search_results") or []
    await agent._emit_targeting_craft(
        stream,
        craft_id,
        "Targeting Recommendations",
        result,
        search_results=search_results,
    )

    removed_summary = ", ".join(removed_names) if removed_names else (target_id or name or category)
    return ToolResult(
        success=True,
        data=result.model_dump(),
        summary=f"Removed targeting segment(s): '{removed_summary}'.",
    )


delete_targeting_segment = ToolDefinition(
    name="delete_targeting_segment",
    description=(
        "Delete one or more targeting segments from the current selection. "
        "Use this ONLY when the user asks conversationally to remove, delete, or clear segments, "
        "for example: 'remove the Real Estate segment', 'delete all behaviors', 'clear everything'. "
        "Do NOT call this when the user is using the craft panel delete button — "
        "that action is handled by the UI directly via the REST API."
    ),
    display_name="Delete Targeting Segment",
    parameters=[
        ToolParameter(
            name="target_id",
            type="string",
            description="Exact Meta targeting category ID of the segment to delete.",
            required=False,
        ),
        ToolParameter(
            name="name",
            type="string",
            description=(
                "Name or partial name of the segment to delete (case-insensitive substring match). "
                "Use 'all' or 'everything' to clear all segments."
            ),
            required=False,
        ),
        ToolParameter(
            name="category",
            type="string",
            description=(
                "Delete all segments of this category type: 'interests', 'behaviors', or 'demographics'."
            ),
            required=False,
        ),
    ],
    execute=_delete_targeting_segment,
)


TARGETING_TOOLS = [suggest_meta_targeting]
