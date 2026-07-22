"""Endpoint for AppBuilderV4Agent at /api/ai/appbuilderv4/chat.

Mirrors the v3 appbuilder router's surface so existing UI / drivers can
target v4 with only a URL change. v4 coexists with v3 until v4 proves out.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.agents.appbuilder.router import AppUserAuth  # reuse the v3 model
from app.core.base_auth import require_auth_context
from app.core.base_router import (
    ChatAttachment,
    build_image_blocks,
    create_common_routes,
    stream_agent_response,
)
from app.core.session import AuthContext, BaseSession
from app.services.security import ALLOWED_AI_APPS
from app.services.session_manager import get_session_manager

logger = logging.getLogger(__name__)

router = APIRouter()
create_common_routes(router, agent_name="appbuilderv4")

_agent = None


def set_appbuilderv4_agent(agent) -> None:
    global _agent
    _agent = agent


async def require_ai_auth_context(
    auth: AuthContext = Depends(require_auth_context),
) -> AuthContext:
    verified_app = auth.access_app_code
    if not verified_app or verified_app.lower() not in ALLOWED_AI_APPS:
        raise HTTPException(
            status_code=403,
            detail="AI features are only available in appbuilder or sitezump applications.",
        )
    return auth


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    app_code: Optional[str] = None
    model: Optional[str] = None
    attachments: Optional[List[ChatAttachment]] = None
    app_user: Optional[AppUserAuth] = None


@router.post("/chat")
async def chat(body: ChatRequest, auth: AuthContext = Depends(require_ai_auth_context)):
    """Stream an appbuilder v4 agent response as SSE."""
    if _agent is None:
        raise HTTPException(status_code=503, detail="AppBuilderV4 agent not initialized")

    if body.app_code:
        auth.app_code = body.app_code

    session = BaseSession(agent_name="appbuilderv4")
    if body.app_code:
        session.context["app_code"] = body.app_code

    if body.app_user is not None:
        session.set_app_user(body.app_user.model_dump(exclude_none=True))

    await session.get_or_create(body.session_id, auth)

    if not body.session_id:
        title = body.message[:100].strip()
        if title:
            await get_session_manager().update_session_title(
                session.session_id, title, auth.user_id
            )

    image_blocks = build_image_blocks(body.attachments, _agent._provider_name) if body.attachments else None
    return stream_agent_response(_agent, body.message, session, image_blocks, model_override=body.model)
