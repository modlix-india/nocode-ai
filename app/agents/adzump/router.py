"""Adzump router — chat endpoint and scheduled optimization trigger.

Common endpoints (models, sessions) are registered via create_common_routes().
The /chat endpoint contains adzump-specific logic.

NOTE: The optimization/scheduler endpoint is colocated here for convenience
but is operationally a cross-cutting (admin/cron) concern. For clearer
separation of responsibilities and easier permissions/auditing, consider
moving scheduled task endpoints to a dedicated admin or scheduler router.
"""

from __future__ import annotations

from dataclasses import asdict
import logging
from typing import Optional, List

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.base_auth import require_auth_context
from app.agents.adzump.agent import AdzumpAgent

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

    if not (body.session_id or "").strip():
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
            for a in body.attachments if a.type == "image" and a.data
        ]

    return stream_agent_response(agent, body.message, session, image_blocks, model_override=body.model)


# ---------------------------------------------------------------------------
# Optimization API — called by nightly scheduler
# ---------------------------------------------------------------------------


@router.post("/optimize/recommendations")
async def generate_recommendations_batch(
    auth: AuthContext = Depends(require_auth_context),
):
    """Trigger recommendation generation (keywords, budget, bidding etc) for all accounts/campaigns.

    Called by the nightly scheduler.
    """
    from app.agents.adzump.agents.optimization.runner import (
        get_scheduled_optimization_runner,
    )

    auth_headers = auth.to_headers()
    summary = await get_scheduled_optimization_runner().run_all(
        client_code=auth.client_code,
        auth_headers=auth_headers,
        auth=auth,
    )
    return {"status": "success", "data": asdict(summary)}
