"""Detailed targeting tool - launches DetailedTargetingAgent sub-agent to generate
Meta Ads detailed targeting suggestions (interests, demographics, behaviors).
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.core.streaming import pre_emit_agent_started

# pyrefly: ignore [missing-import]
from app.agents.adzump.adapters.meta.targeting_adapter import (
    TargetingAdapter as MetaTargetingAdapter,
)
from app.agents.adzump.agents.campaign.meta.agent import get_detailed_targeting_agent
from app.agents.adzump.agents.campaign.meta.models import (
    MetaTargetingSuggestionResult,
    TargetingEntity,
    map_type_to_category,
)

logger = logging.getLogger(__name__)

# Shared adapter instance for general searches
_adapter = MetaTargetingAdapter()


def _build_suggestion_result(
    targeting: dict[str, Any],
) -> MetaTargetingSuggestionResult:
    """Helper to parse raw segment lists into structures."""

    def _parse(items: list[Any]) -> list[TargetingEntity]:
        return [
            TargetingEntity(**item) if isinstance(item, dict) else item
            for item in items
        ]

    return MetaTargetingSuggestionResult(
        interests=_parse(targeting.get("interests", [])),
        demographics=_parse(targeting.get("demographics", [])),
        behaviors=_parse(targeting.get("behaviors", [])),
    )


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

        summary = (
            f"Suggested detailed targeting segments: "
            f"{len(result.interests)} interests, {len(result.demographics)} demographics, "
            f"and {len(result.behaviors)} behaviors. Check the side panel for details."
        )
        if explanation:
            summary += f"\n\nAnalyst's Explanation:\n{explanation}"

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
        "Generate an ENTIRELY NEW list of Meta Ads detailed targeting suggestions (interests, demographics, behaviors) "
        "using the AI Targeting Analyst. "
        "Run this ONLY when the user explicitly asks for a full strategy or targeting recommendations. "
        "Do NOT use this tool if the user is just searching for a specific keyword to add to their existing list."
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


async def _modify_meta_targeting(
    params: dict[str, Any], context: dict[str, Any]
) -> ToolResult:
    """Add, delete, or search detailed targeting segments from the session context."""
    action = (params.get("action") or "").strip().lower()
    target_id = (params.get("target_id") or "").strip()
    name = (params.get("name") or "").strip()
    category = (params.get("category") or "").strip().lower()
    query = (params.get("query") or "").strip()

    session_ctx = context.get("session_context", {}) or {}
    stream = context.get("event_stream")
    session = context.get("_session")
    auth = context.get("auth")

    if not session:
        return ToolResult(
            success=False, error="No active session found in tool context."
        )

    targeting = session_ctx.setdefault("detailed_targeting", {})
    if not isinstance(targeting, dict):
        targeting = {}
        session_ctx["detailed_targeting"] = targeting

    # Models are imported at the module level

    agent = get_detailed_targeting_agent()
    craft_id = f"detailed_targeting_{session.session_id}"

    if action == "delete":
        if not target_id:
            return ToolResult(
                success=False, error="target_id is required for delete action."
            )

        removed_name = ""
        for key in ["interests", "demographics", "behaviors"]:
            orig_list = targeting.get(key) or []
            new_list = []
            for item in orig_list:
                item_id = (
                    item.get("id")
                    if isinstance(item, dict)
                    else getattr(item, "id", None)
                )
                if str(item_id) == str(target_id):
                    removed_name = (
                        item.get("name")
                        if isinstance(item, dict)
                        else getattr(item, "name", "")
                    )
                else:
                    new_list.append(item)
            targeting[key] = new_list

        result = _build_suggestion_result(targeting)
        # Stash updated targeting result back into session context for LLM visibility
        session_ctx["detailed_targeting"] = result.model_dump()

        search_results = session_ctx.get("detailed_targeting_search_results") or []
        await agent._emit_targeting_craft(
            stream,
            craft_id,
            "Targeting Recommendations",
            result,
            search_results=search_results,
        )
        return ToolResult(
            success=True,
            data=result.model_dump(),
            summary=f"Removed targeting segment '{removed_name or target_id}' successfully.",
        )

    elif action == "add":
        if not target_id:
            return ToolResult(
                success=False, error="target_id is required for add action."
            )

        cat_key = map_type_to_category(category or "interests")

        orig_list = targeting.setdefault(cat_key, [])
        exists = False
        for item in orig_list:
            item_id = (
                item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
            )
            if str(item_id) == str(target_id):
                exists = True
                break

        if not exists:
            # Restore full metadata from search results if available
            search_results = session_ctx.get("detailed_targeting_search_results") or []
            matched_search_item = None
            for item in search_results:
                if str(item.get("id")) == str(target_id):
                    matched_search_item = item
                    break

            if matched_search_item:
                new_entity = TargetingEntity(**matched_search_item)
            else:
                new_entity = TargetingEntity(
                    id=target_id,
                    name=name or f"ID: {target_id}",
                    type=cat_key,
                    category=cat_key,
                )
            orig_list.append(new_entity.model_dump())

        # Clear search results upon successful add
        session_ctx.pop("detailed_targeting_search_results", None)

        result = _build_suggestion_result(targeting)
        # Stash updated targeting result back into session context for LLM visibility
        session_ctx["detailed_targeting"] = result.model_dump()

        await agent._emit_targeting_craft(
            stream, craft_id, "Targeting Recommendations", result, search_results=[]
        )
        return ToolResult(
            success=True,
            data=result.model_dump(),
            summary=f"Added targeting segment '{name or target_id}' successfully.",
        )

    elif action == "search":
        if not query:
            return ToolResult(
                success=False, error="query is required for search action."
            )
        if not auth:
            return ToolResult(
                success=False,
                error="Missing authentication credentials in tool context.",
            )

        spec = session_ctx.get("campaign_spec") or {}
        ad_account_id = spec.get("account")
        if not ad_account_id:
            return ToolResult(
                success=False,
                error="Meta Ad Account ID is missing from campaign specs.",
            )

        try:
            entities = await _adapter.search(
                client_code=auth.client_code,
                auth_headers=auth.to_headers(),
                account_id=ad_account_id,
                query=query,
            )
            search_results = [item.model_dump() for item in entities]
            session_ctx["detailed_targeting_search_results"] = search_results

            result = _build_suggestion_result(targeting)

            await agent._emit_targeting_craft(
                stream,
                craft_id,
                "Targeting Recommendations",
                result,
                search_results=search_results,
            )
            return ToolResult(
                success=True,
                data={"searchResults": search_results},
                summary=f"Found {len(search_results)} detailed targeting segments matching '{query}'.",
            )
        except Exception as e:
            logger.error("Meta detailed targeting search failed: %s", e)
            return ToolResult(
                success=False, error=f"Search failed: {type(e).__name__}: {e}"
            )

    return ToolResult(success=False, error=f"Unknown action: {action}")


modify_meta_targeting = ToolDefinition(
    name="modify_meta_targeting",
    description=(
        "Modify the CURRENT Meta Ads detailed targeting list. "
        "Use this tool when the user wants to: "
        "1. 'add' a specific segment by ID, "
        "2. 'delete' a segment by ID, or "
        "3. 'search' the Meta API for specific keywords (e.g. 'search for marketing') to see options to add. "
        "MUST use this for all manual searches instead of generating new suggestions."
    ),
    display_name="Modify Meta Targeting",
    parameters=[
        ToolParameter(
            name="action",
            type="string",
            description="The action to perform: 'add', 'delete', or 'search'.",
            required=True,
        ),
        ToolParameter(
            name="target_id",
            type="string",
            description="Meta targeting category ID of the segment to add or delete (e.g. '6003123456789').",
            required=False,
        ),
        ToolParameter(
            name="name",
            type="string",
            description="The name of the targeting segment (optional, used when adding).",
            required=False,
        ),
        ToolParameter(
            name="category",
            type="string",
            description="The targeting category: 'interests', 'demographics', or 'behaviors' (used when adding).",
            required=False,
        ),
        ToolParameter(
            name="query",
            type="string",
            description="The search query text (required when action is 'search').",
            required=False,
        ),
    ],
    execute=_modify_meta_targeting,
)


TARGETING_TOOLS = [suggest_meta_targeting, modify_meta_targeting]
