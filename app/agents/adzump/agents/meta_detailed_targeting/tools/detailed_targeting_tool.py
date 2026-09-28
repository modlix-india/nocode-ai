"""Detailed targeting tool - launches DetailedTargetingAgent sub-agent to generate
Meta Ads detailed targeting suggestions (interests, demographics, behaviors).

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
)

logger = logging.getLogger(__name__)


# suggest_meta_targeting — spawns the AI sub-agent

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

    # 2. Resolve business description summary (gate on summary actually used by agent, not url)
    summary = (
        (session_ctx.get("product_data") or {}).get("summary")
        or (session_ctx.get("product_profile") or {}).get("summary")
        or ""
    )
    if not summary:
        return ToolResult(
            success=False,
            error="No business description found. Please perform website or product analysis first.",
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
        "those are handled by the craft panel directly. "
        "Do NOT use this for any request to remove, delete, or clear targeting segments — "
        "use `delete_targeting_segment` directly for those instead."
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


# delete_targeting_segment — LLM-callable for conversational deletes

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
    clear_all_param = bool(params.get("clear_all", False))
    clear_all = clear_all_param or (name.lower() in ("all", "everything", "all segments") if name else False)

    if not target_id and not name and not category and not clear_all:
        return ToolResult(
            success=False,
            error="Provide at least one of: target_id, name, category, or clear_all to identify what to delete.",
        )

    session_ctx = context.get("session_context", {}) or {}
    session = context.get("_session")

    if not session:
        return ToolResult(success=False, error="No active session found in tool context.")

    targeting = session_ctx.setdefault("detailed_targeting", {})
    if not isinstance(targeting, dict):
        targeting = {}
        session_ctx["detailed_targeting"] = targeting

    orig_list = targeting.get("entities") or []
    new_list = []
    removed_names: list[str] = []
    removed_ids: list[str] = []

    import re

    def _clean_name(s: str) -> str:
        # Strip trailing parenthetical category tag like '(design)' or '(publication)'
        cleaned = re.sub(r"\s*\([^)]*\)$", "", s).strip().lower()
        return cleaned or s.strip().lower()

    target_clean = _clean_name(name) if name else ""

    # Check for exact normalized name match first across all items
    has_exact_name_match = (
        bool(name) and not clear_all and any(
            _clean_name(item.get("name") if isinstance(item, dict) else getattr(item, "name", "")) == target_clean
            for item in orig_list
        )
    )

    for item in orig_list:
        item_id = str(item.get("id") if isinstance(item, dict) else getattr(item, "id", None))
        item_name = item.get("name") if isinstance(item, dict) else getattr(item, "name", "")
        item_type = (item.get("type") if isinstance(item, dict) else getattr(item, "type", "")) or ""
        item_clean = _clean_name(item_name)

        match_id = bool(target_id and str(item_id) == str(target_id))
        match_clear_all = clear_all
        match_category = bool(category and item_type.lower() == category)

        if match_clear_all or match_id or match_category:
            should_remove = True
        elif name:
            if has_exact_name_match:
                # Prefer exact match so 'Interior design (design)' does not delete other interior design items
                should_remove = (item_clean == target_clean or item_name.strip().lower() == name.strip().lower())
            else:
                should_remove = (target_clean in item_clean)
        else:
            should_remove = False

        if should_remove:
            removed_names.append(item_name)
            if item_id:
                removed_ids.append(item_id)
        else:
            new_list.append(item)

    targeting["entities"] = new_list

    # Record tombstones in excluded_ids
    excluded_ids = targeting.setdefault("excluded_ids", [])
    for rid in removed_ids:
        if str(rid) not in excluded_ids:
            excluded_ids.append(str(rid))

    # Remove from user_added_ids if present
    if "user_added_ids" in targeting:
        targeting["user_added_ids"] = [
            uid for uid in targeting["user_added_ids"] if str(uid) not in removed_ids
        ]

    result = MetaTargetingSuggestionResult.from_dict(targeting)
    session_ctx["detailed_targeting"] = result.model_dump()

    # Anchor craft ID to parent session so subagent calls never emit temporary subsession IDs
    stream = context.get("event_stream")
    target_session_id = context.get("_parent_session_id") or getattr(session, "session_id", "")
    if stream and target_session_id:
        craft_id = f"detailed_targeting_{target_session_id}"
        search_results = session_ctx.get("detailed_targeting_search_results") or []
        await get_detailed_targeting_agent()._emit_targeting_craft(
            stream=stream,
            craft_id=craft_id,
            title="Meta Targeting Suggestions",
            result=result,
            search_results=search_results
        )

    # Persist context immediately to DB
    if hasattr(session, "save_context"):
        try:
            await session.save_context()
        except Exception as e:
            logger.warning("[delete_targeting_segment] Failed to save session context: %s", e)

    removed_summary = ", ".join(removed_names) if removed_names else (target_id or name or category)
    return ToolResult(
        success=True,
        data=result.model_dump(),
        summary=f"Removed targeting segment(s): '{removed_summary}'.",
    )


delete_targeting_segment = ToolDefinition(
    name="delete_targeting_segment",
    description=(
        "Remove targeting segments from the current selection — by segment ID, name, category, or clear all. "
        "Use this directly whenever the user asks to remove, delete, or clear one or more targeting segments "
        "conversationally (e.g. 'remove Real Estate', 'delete all behaviors', 'clear everything'). "
        "This operates in-memory with no API calls and no sub-agent — "
        "do NOT route deletion requests through suggest_meta_targeting."
    ),
    display_name="Delete Targeting Segment",
    parameters=[
        ToolParameter(
            name="target_id",
            type="string",
            description="Specific Meta segment ID to delete (e.g. '6003139266661').",
            required=False,
        ),
        ToolParameter(
            name="name",
            type="string",
            description="Segment name or keyword to delete (case-insensitive substring match).",
            required=False,
        ),
        ToolParameter(
            name="clear_all",
            type="boolean",
            description=(
                "If true, removes ALL detailed targeting segments. "
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


# Orchestrator-facing targeting tools (suggest_meta_targeting, delete_targeting_segment)
ORCHESTRATOR_TARGETING_TOOLS = [suggest_meta_targeting, delete_targeting_segment]
