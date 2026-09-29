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
from typing import Any, Optional

from pydantic import BaseModel, model_validator, ValidationError

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.core.streaming import pre_emit_agent_started

from app.agents.adzump.agents.meta_detailed_targeting.agent import (
    get_detailed_targeting_agent,
)
from app.agents.adzump.agents.meta_detailed_targeting.models import (
    MetaTargetingSuggestionResult,
    DeleteSegmentArgs,
)
from app.agents.adzump.agents.meta_detailed_targeting.mutation import apply_targeting_edit

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

def _handle_clear_all(session_ctx: dict) -> tuple[bool, str, list[str]]:
    success, msg = apply_targeting_edit("clear_all", "", session_ctx)
    return success, msg, ["all segments"] if success else []

def _handle_delete_category(category: str, session_ctx: dict) -> tuple[bool, str, list[str]]:
    removed_items = []
    dt = session_ctx.get("detailed_targeting", {})
    entities = list(dt.get("entities") or [])
    
    for e in entities:
        e_type = (e.get("type") if isinstance(e, dict) else getattr(e, "type", "")) or ""
        if e_type.lower() == category.lower():
            e_id = e.get("id") if isinstance(e, dict) else getattr(e, "id", None)
            if e_id:
                success, _ = apply_targeting_edit("delete", str(e_id), session_ctx)
                if success:
                    e_name = e.get("name") if isinstance(e, dict) else getattr(e, "name", "")
                    removed_items.append(e_name)
                    
    if removed_items:
        return True, f"Cleared category: {category}", removed_items
    return False, f"No segments found in category: {category}", []

def _handle_delete_specific(target_id: Optional[str], name: Optional[str], session_ctx: dict) -> tuple[bool, str, list[str]]:
    success = False
    msg = ""
    matched_term = None

    if target_id:
        success, msg = apply_targeting_edit("delete", target_id, session_ctx)
        if success:
            matched_term = target_id

    # Fallback to name if target_id was not provided OR if target_id resolution failed
    if not success and name:
        success, msg = apply_targeting_edit("delete", name, session_ctx)
        if success:
            matched_term = name

    if success:
        return True, msg, [matched_term or target_id or name]
    return False, msg, []

async def _delete_targeting_segment(
    params: dict[str, Any], context: dict[str, Any]
) -> ToolResult:
    session = context.get("_session")
    if not session:
        return ToolResult(success=False, error="No active session found in tool context.")

    session_ctx = context.get("session_context", {}) or {}

    # 1. Pydantic validation & precedence sanitization
    try:
        args = DeleteSegmentArgs(**params)
    except ValidationError as e:
        return ToolResult(success=False, error=str(e))

    if not args.target_id and not args.name and not args.category and not args.clear_all:
        return ToolResult(
            success=False,
            error="Provide at least one of: target_id, name, category, or clear_all to identify what to delete."
        )

    # 2. Dispatcher
    success, error_msg, removed_items = False, "", []
    
    if args.clear_all:
        success, error_msg, removed_items = _handle_clear_all(session_ctx)
    elif args.target_id or args.name:
        success, error_msg, removed_items = _handle_delete_specific(args.target_id, args.name, session_ctx)
    elif args.category:
        success, error_msg, removed_items = _handle_delete_category(args.category, session_ctx)

    if not success and not removed_items:
        return ToolResult(success=False, error=error_msg)

    # Re-build result to emit
    targeting = session_ctx.get("detailed_targeting", {})
    result = MetaTargetingSuggestionResult.from_dict(targeting)
    session_ctx["detailed_targeting"] = result.model_dump()

    # Anchor craft ID to parent session so subagent calls never emit temporary subsession IDs
    stream = context.get("event_stream")
    ephemeral = context.get("targeting_ephemeral", {})
    target_session_id = ephemeral.get("parent_session_id") or context.get("_parent_session_id") or getattr(session, "session_id", "")
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

    removed_summary = ", ".join(removed_items) if removed_items else (args.target_id or args.name or args.category)
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
