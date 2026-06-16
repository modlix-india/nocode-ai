"""Adzump router — chat endpoint.

Common endpoints (models, sessions) are registered via create_common_routes().
Only the /chat endpoint with adzump-specific logic lives here.
"""

from __future__ import annotations

import logging
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.base_auth import require_auth_context
from app.core.base_router import (
    ChatAttachment,
    build_image_blocks,
    create_common_routes,
    stream_agent_response,
)
from app.core.session import BaseSession, AuthContext
from app.services.session_manager import get_session_manager

logger = logging.getLogger(__name__)

router = APIRouter()
create_common_routes(router, agent_name="adzump")

from app.agents.adzump.agent import AdzumpAgent


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    model: Optional[str] = None
    attachments: Optional[List[ChatAttachment]] = None


@router.post("/chat")
async def chat(body: ChatRequest, auth: AuthContext = Depends(require_auth_context)):
    """Stream an adzump agent response as SSE."""
    agent = AdzumpAgent.get_instance()

    session = BaseSession(agent_name="adzump")
    await session.get_or_create(body.session_id, auth)

    if not body.session_id:
        title = body.message[:100].strip()
        if title:
            await get_session_manager().update_session_title(
                session.session_id, title, auth.user_id
            )

    image_blocks = build_image_blocks(body.attachments) if body.attachments else None

    # v9 I-0 · stash raw image uploads so save_uploaded_assets can persist them
    # as campaign assets. build_image_blocks only formats them for LLM vision
    # (then drops the bytes); the ingest tool needs the raw base64. Overwrites
    # any prior stash — only this turn's uploads are pending ingest.
    if body.attachments:
        session.context["_pending_uploads"] = [
            {"data": a.data, "mime": a.mime_type, "name": a.name}
            for a in body.attachments
            if a.type == "image" and a.data
        ]

    return stream_agent_response(
        agent, body.message, session, image_blocks, model_override=body.model
    )


@router.get("/sessions/{session_id}/target-locations/search")
async def search_target_locations(
    session_id: str,
    q: str,
    platform: str = "google",
    auth: AuthContext = Depends(require_auth_context),
):
    session = BaseSession(agent_name="adzump")
    await session.get_or_create(session_id, auth)

    from app.agents.adzump.services.geo.search import search_autocomplete_locations

    loc_meta = session.context.get("_location_meta") or {}
    country_code = loc_meta.get("country_code") or "IN"

    try:
        return await search_autocomplete_locations(
            q=q,
            platform=platform,
            client_code=auth.client_code,
            auth_headers=auth.to_headers(),
            session_context=session.context,
            country_code=country_code,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
