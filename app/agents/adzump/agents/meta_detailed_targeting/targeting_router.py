"""Detailed targeting HTTP routes - UI helpers for the targeting panel.

These are *UI helpers*, not part of the orchestrator's chat flow. The craft
panel calls these endpoints directly for keyword search, adding a segment, and
deleting a segment. The orchestrator's LLM never invokes them.

Endpoint summary:
  GET  /sessions/{session_id}/detailed-targeting/search?q={keyword}
       Search Meta targeting catalog. Stashes results in session. Returns list.

  DELETE /sessions/{session_id}/detailed-targeting/segments/{segment_id}
       Remove a segment by ID from the current selection. Persists updated state.

  POST /sessions/{session_id}/detailed-targeting/segments
       Add a segment (from prior search results) by ID. Persists updated state.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.core.base_auth import require_auth_context, AuthContext
from app.services.session_manager import get_session_manager
from app.agents.adzump.adapters.meta.targeting_adapter import TargetingAdapter
from app.agents.adzump.agents.meta_detailed_targeting.models import (
    TargetingEntity,
    resolve_ad_account_id,
)
from app.agents.adzump.agents.meta_detailed_targeting.tools.targeting_tools import (
    TOTAL_TARGETING_LIMIT,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Detailed Targeting Search"])

_adapter = TargetingAdapter()



# GET /sessions/{session_id}/detailed-targeting/search
@router.get("/sessions/{session_id}/detailed-targeting/search")
async def search_targeting_options(
    session_id: str,
    q: str = Query(..., min_length=1),
    auth: AuthContext = Depends(require_auth_context),
):
    """Typeahead search for Meta targeting segments matching a keyword.

    """
    _sm = get_session_manager()
    _ai_session = await _sm.get_session(session_id)
    if not _ai_session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Verify tenant authorization ownership
    if _ai_session.user_id != auth.user_id:
        raise HTTPException(status_code=403, detail="Access denied: session ownership mismatch")

    session_ctx: dict[str, Any] = {}
    if _ai_session.context_json:
        try:
            session_ctx = json.loads(_ai_session.context_json)
        except (json.JSONDecodeError, TypeError):
            logger.warning("[targeting_router] Could not parse context_json for session=%s", session_id)

    ad_account_id = resolve_ad_account_id(session_ctx)
    if not ad_account_id:
        raise HTTPException(
            status_code=400,
            detail="Meta ad_account_id not found in session context.",
        )

    try:
        entities = await _adapter.search(
            client_code=auth.client_code,
            auth_headers=auth.to_headers(),
            account_id=ad_account_id,
            q=q,
        )

        results = []
        for e in entities:
            size = ""
            if e.audience_size_lower_bound and e.audience_size_upper_bound:
                size = f"{e.audience_size_lower_bound:,} – {e.audience_size_upper_bound:,}"
            elif e.audience_size_lower_bound:
                size = f"Over {e.audience_size_lower_bound:,}"

            results.append({
                "id": e.id,
                "name": e.name,
                "type": e.type,
                "size": size,
            })

        # Stash full entity metadata capped at maximum 50 recent items
        existing = session_ctx.get("detailed_targeting_search_results") or []
        seen_ids = {str(item.get("id")) for item in existing if item.get("id")}
        merged = list(existing)
        for e in entities:
            e_id = str(e.id)
            if e_id and e_id not in seen_ids:
                seen_ids.add(e_id)
                merged.append(e.model_dump())
        new_search_results = merged[-50:]
        session_ctx["detailed_targeting_search_results"] = new_search_results
        await _sm.update_session_context(
            session_id,
            context_json=json.dumps(session_ctx),
            user_id=auth.user_id,
        )

        logger.info(
            "[targeting_router] search q=%r → %d results (session=%s)",
            q, len(results), session_id,
        )
        return results

    except Exception as exc:
        logger.exception("[targeting_router] search failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Targeting search failed: {exc}")


# DELETE /sessions/{session_id}/detailed-targeting/segments/{segment_id}
@router.delete("/sessions/{session_id}/detailed-targeting/segments/{segment_id}")
async def delete_targeting_segment(
    session_id: str,
    segment_id: str,
    auth: AuthContext = Depends(require_auth_context),
):
    """Remove a targeting segment by ID from the current selection.

    """
    _sm = get_session_manager()
    _ai_session = await _sm.get_session(session_id)
    if not _ai_session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Verify tenant authorization ownership
    if _ai_session.user_id != auth.user_id:
        raise HTTPException(status_code=403, detail="Access denied: session ownership mismatch")

    session_ctx: dict[str, Any] = {}
    if _ai_session.context_json:
        try:
            session_ctx = json.loads(_ai_session.context_json)
        except (json.JSONDecodeError, TypeError):
            logger.warning("[targeting_router] Could not parse context_json for session=%s", session_id)
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
    await _sm.update_session_context(
        session_id,
        context_json=json.dumps(session_ctx),
        user_id=auth.user_id,
    )

    logger.info(
        "[targeting_router] delete segment_id=%s removed=%d remaining=%d (session=%s)",
        segment_id, removed_count, len(new_list), session_id,
    )
    return {
        "success": True,
        "removed": removed_count,
        "remaining": len(new_list),
    }


# POST /sessions/{session_id}/detailed-targeting/segments
class AddSegmentRequest(BaseModel):
    id: str
    name: str
    type: str | None = None


@router.post("/sessions/{session_id}/detailed-targeting/segments")
async def add_targeting_segment(
    session_id: str,
    body: AddSegmentRequest,
    auth: AuthContext = Depends(require_auth_context),
):
    """Add a targeting segment to the current selection.

    Validates the segment ID against Meta's targetingvalidation API before
    adding it, so arbitrary or fabricated IDs are rejected. Uses Meta's
    authoritative response (correct type, name, audience_size) rather than
    trusting the client body. Enforces TOTAL_TARGETING_LIMIT (60 segments).
    """
    _sm = get_session_manager()
    _ai_session = await _sm.get_session(session_id)
    if not _ai_session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Verify tenant authorization ownership
    if _ai_session.user_id != auth.user_id:
        raise HTTPException(status_code=403, detail="Access denied: session ownership mismatch")

    session_ctx: dict[str, Any] = {}
    if _ai_session.context_json:
        try:
            session_ctx = json.loads(_ai_session.context_json)
        except (json.JSONDecodeError, TypeError):
            logger.warning("[targeting_router] Could not parse context_json for session=%s", session_id)
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

    # Enforce global segment limit before doing any Meta API work
    if len(orig_list) >= TOTAL_TARGETING_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot add segment: maximum of {TOTAL_TARGETING_LIMIT} segments already selected.",
        )

    # Resolve Meta ad account ID — required for validation
    ad_account_id = resolve_ad_account_id(session_ctx)
    if not ad_account_id:
        raise HTTPException(
            status_code=400,
            detail="Meta ad_account_id not found in session context.",
        )

    # Build a candidate entity.
    search_results = session_ctx.get("detailed_targeting_search_results") or []
    matched = next(
        (item for item in search_results if str(item.get("id")) == str(body.id)),
        None,
    )

    if matched:
        candidate = TargetingEntity.from_meta(matched)
    else:
        candidate = TargetingEntity.from_meta({
            "id": body.id,
            "name": body.name,
            "type": body.type or "interests",
        })

    if not candidate:
        raise HTTPException(status_code=400, detail="Invalid segment data in request.")

    # Validate against Meta's targetingvalidation API.
    # This rejects fabricated, deprecated, or inactive segment IDs.
    # The returned entity carries Meta's authoritative type, name, and audience_size.
    try:
        valid_entities = await _adapter.validate(
            client_code=auth.client_code,
            auth_headers=auth.to_headers(),
            account_id=ad_account_id,
            entities=[candidate],
        )
    except Exception as exc:
        logger.exception("[targeting_router] Meta validation failed for segment %s", body.id)
        raise HTTPException(
            status_code=502,
            detail=f"Could not validate segment with Meta API: {exc}",
        )

    if not valid_entities:
        raise HTTPException(
            status_code=400,
            detail=f"Segment '{body.id}' is not a valid or active Meta targeting segment.",
        )

    # Use the validated entity — has Meta's correct type, name, and audience_size
    entity = valid_entities[0]
    orig_list.append(entity.model_dump())
    await _sm.update_session_context(
        session_id,
        context_json=json.dumps(session_ctx),
        user_id=auth.user_id,
    )

    logger.info(
        "[targeting_router] add segment_id=%s name=%r type=%s total=%d (session=%s)",
        entity.id, entity.name, entity.type, len(orig_list), session_id,
    )
    return {
        "success": True,
        "added": True,
        "total": len(orig_list),
    }
