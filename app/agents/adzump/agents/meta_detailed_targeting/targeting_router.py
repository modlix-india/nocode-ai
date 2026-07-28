"""Detailed targeting HTTP routes - UI helpers for the targeting panel.

These are *UI helpers*, not part of the orchestrator's chat flow. The craft
panel calls these endpoints directly for keyword search, adding a segment, and
deleting a segment. The orchestrator's LLM never invokes them.

Why not through the LLM?
  - Search fires on every button press. An LLM roundtrip adds ~5-10s of lag
    and ~$0.01 per search in token costs. Meta's targetingsearch API does this
    in ~150ms.
  - Delete is deterministic: the user clicked a specific chip's trash icon.
    Routing a known segment ID through the LLM only to call modify_meta_targeting
    adds an unnecessary LLM roundtrip (5-10s, token cost) for a pure list mutation.
  - Add from search is deterministic: the segment ID is known from the search result.

The LLM sub-agent (DetailedTargetingAgent) is reserved for strategic requests
like "expand my audience to include luxury homeowners".

Why a separate HTTP endpoint vs. sending through the chat?
  The frontend cannot call the Meta Ads Graph API directly — it needs the user's
  auth headers and the client_code, which only the backend has. The backend proxies
  the call. The result updates the session state and re-emits the craft panel.

Endpoint summary:
  GET  /sessions/{session_id}/detailed-targeting/search?q={keyword}
       Search Meta targeting catalog. Stashes results in session. Returns list.

  DELETE /sessions/{session_id}/detailed-targeting/segments/{segment_id}
       Remove a segment by ID from the current selection. Re-emits craft.

  POST /sessions/{session_id}/detailed-targeting/segments
       Add a segment (from prior search results) by ID. Re-emits craft.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.core.base_auth import require_auth_context, AuthContext
from app.core.session import BaseSession
from app.agents.adzump.adapters.meta.targeting_adapter import TargetingAdapter
from app.agents.adzump.agents.meta_detailed_targeting.models import (
    MetaTargetingSuggestionResult,
    TargetingEntity,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Detailed Targeting Search"])

_adapter = TargetingAdapter()


def _resolve_account_id(session_ctx: dict[str, Any]) -> str:
    """Resolve Meta ad account ID from session context."""
    account_id = (session_ctx.get("ad_account_id") or "").strip()
    if account_id:
        return account_id
    spec = session_ctx.get("campaign_spec") or {}
    return (spec.get("account") or "").strip()


def _build_result(targeting: dict[str, Any]) -> MetaTargetingSuggestionResult:
    """Parse raw entity list into MetaTargetingSuggestionResult."""
    all_raw = targeting.get("entities", [])
    entities = [e for e in (TargetingEntity.from_meta(x) for x in all_raw) if e]
    return MetaTargetingSuggestionResult(entities=entities)


async def _rerender_craft(
    session_ctx: dict[str, Any],
    targeting: dict[str, Any],
) -> None:
    """Re-emit the targeting craft panel after a state mutation.

    Currently logs mutations to persist state in the session; the UI reads it
    on the next render.
    """
    try:
        result = _build_result(targeting)
        parent_session_id = session_ctx.get("_parent_session_id") or ""
        craft_id = f"detailed_targeting_{parent_session_id}"

        logger.info(
            "[targeting_router] craft re-render skipped (no active SSE); "
            "state persisted in session — craft_id=%s entities=%d",
            craft_id,
            len(result.entities),
        )

    except Exception as exc:
        logger.warning("[targeting_router] _rerender_craft failed: %s", exc)


# ---------------------------------------------------------------------------
# GET /sessions/{session_id}/detailed-targeting/search
# ---------------------------------------------------------------------------
@router.get("/sessions/{session_id}/detailed-targeting/search")
async def search_targeting_options(
    session_id: str,
    q: str = Query(..., min_length=1),
    auth: AuthContext = Depends(require_auth_context),
):
    """Typeahead search for Meta targeting segments matching a keyword.

    Calls Meta targetingsearch directly — no LLM, no sub-agent.
    Stashes results in session context so the Add endpoint can resolve metadata.
    Returns a compact list: [{id, name, type, size}].
    """
    session = BaseSession(agent_name="adzump")
    await session.get_or_create(session_id, auth)

    account_id = _resolve_account_id(session.context)
    if not account_id:
        raise HTTPException(
            status_code=400,
            detail="Meta Ad Account ID not found in session. Please set up the campaign spec first.",
        )

    try:
        entities = await _adapter.search(
            client_code=auth.client_code,
            auth_headers=auth.to_headers(),
            account_id=account_id,
            query=q,
        )

        results = []
        for e in entities:
            size = None
            if e.audience_size_lower_bound:
                size = e.audience_size_lower_bound
            results.append({
                "id": e.id,
                "name": e.name,
                "type": e.type,
                "size": size,
            })

        # Stash full entity metadata in session so the Add endpoint can retrieve it
        existing = session.context.get("detailed_targeting_search_results") or []
        seen_ids = {str(item.get("id")) for item in existing if item.get("id")}
        merged = list(existing)
        for e in entities:
            e_id = str(e.id)
            if e_id and e_id not in seen_ids:
                seen_ids.add(e_id)
                merged.append(e.model_dump())
        session.context["detailed_targeting_search_results"] = merged
        await session.save_context()

        logger.info(
            "[targeting_router] search q=%r → %d results (session=%s)",
            q, len(results), session_id,
        )
        return results

    except Exception as exc:
        logger.exception("[targeting_router] search failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Targeting search failed: {exc}")


# ---------------------------------------------------------------------------
# DELETE /sessions/{session_id}/detailed-targeting/segments/{segment_id}
# ---------------------------------------------------------------------------
@router.delete("/sessions/{session_id}/detailed-targeting/segments/{segment_id}")
async def delete_targeting_segment(
    session_id: str,
    segment_id: str,
    auth: AuthContext = Depends(require_auth_context),
):
    """Remove a targeting segment by ID from the current selection.

    No LLM involved. Instant list mutation + session save.
    The craft panel re-renders from state on the next UI reconnect.
    """
    session = BaseSession(agent_name="adzump")
    await session.get_or_create(session_id, auth)

    session_ctx = session.context
    targeting = session_ctx.setdefault("detailed_targeting", {})
    if not isinstance(targeting, dict):
        targeting = {}
        session_ctx["detailed_targeting"] = targeting

    orig_list = targeting.get("entities") or []
    new_list = [
        item for item in orig_list
        if str(item.get("id") if isinstance(item, dict) else getattr(item, "id", None))
        != str(segment_id)
    ]
    removed_count = len(orig_list) - len(new_list)
    targeting["entities"] = new_list

    result = _build_result(targeting)
    session_ctx["detailed_targeting"] = result.model_dump()
    await session.save_context()

    await _rerender_craft(session_ctx, result.model_dump())

    logger.info(
        "[targeting_router] delete segment_id=%s removed=%d (session=%s)",
        segment_id, removed_count, session_id,
    )
    return {
        "success": True,
        "removed": removed_count,
        "remaining": len(new_list),
    }


# ---------------------------------------------------------------------------
# POST /sessions/{session_id}/detailed-targeting/segments
# ---------------------------------------------------------------------------
class AddSegmentRequest(BaseModel):
    id: str
    name: str
    type: str | None = None
    size: int | None = None


@router.post("/sessions/{session_id}/detailed-targeting/segments")
async def add_targeting_segment(
    session_id: str,
    body: AddSegmentRequest,
    auth: AuthContext = Depends(require_auth_context),
):
    """Add a targeting segment to the current selection.

    Looks up full metadata from the session's stashed search results so audience_size
    and other fields are preserved. Falls back to the request body if not found.
    No LLM involved. Instant list mutation + session save.
    """
    session = BaseSession(agent_name="adzump")
    await session.get_or_create(session_id, auth)

    session_ctx = session.context
    targeting = session_ctx.setdefault("detailed_targeting", {})
    if not isinstance(targeting, dict):
        targeting = {}
        session_ctx["detailed_targeting"] = targeting

    orig_list = targeting.setdefault("entities", [])

    # Prevent duplicates
    if any(
        str(item.get("id") if isinstance(item, dict) else getattr(item, "id", None))
        == str(body.id)
        for item in orig_list
    ):
        return {"success": True, "added": False, "message": "Segment already in selection."}

    # Try to restore full metadata from stashed search results
    search_results = session_ctx.get("detailed_targeting_search_results") or []
    matched = next(
        (item for item in search_results if str(item.get("id")) == str(body.id)),
        None,
    )

    if matched:
        entity = TargetingEntity.from_meta(matched)
    else:
        # Fallback: build a minimal entity from the request body
        entity = TargetingEntity.from_meta({
            "id": body.id,
            "name": body.name,
            "type": body.type or "interests",
        })

    if entity:
        orig_list.append(entity.model_dump())

    result = _build_result(targeting)
    session_ctx["detailed_targeting"] = result.model_dump()
    await session.save_context()

    await _rerender_craft(session_ctx, result.model_dump())

    logger.info(
        "[targeting_router] add segment_id=%s name=%r (session=%s)",
        body.id, body.name, session_id,
    )
    return {
        "success": True,
        "added": True,
        "total": len(orig_list),
    }
