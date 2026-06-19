"""AppBuilder router — chat endpoint.

Common endpoints (models, sessions) are registered via create_common_routes().
Only the /chat endpoint with appbuilder-specific logic lives here.
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
from app.services.security import ALLOWED_AI_APPS

logger = logging.getLogger(__name__)

router = APIRouter()
create_common_routes(router, agent_name="appbuilder")

_agent = None


def set_appbuilder_agent(agent) -> None:
    """Set the AppBuilderAgent instance (called from main.py lifespan)."""
    global _agent
    _agent = agent


async def require_ai_auth_context(
    auth: AuthContext = Depends(require_auth_context),
) -> AuthContext:
    """Verify access app is AI-enabled (appbuilder/sitezump)."""
    verified_app = auth.access_app_code
    if not verified_app or verified_app.lower() not in ALLOWED_AI_APPS:
        raise HTTPException(
            status_code=403,
            detail="AI features are only available in appbuilder or sitezump applications.",
        )
    return auth


class AppUserAuth(BaseModel):
    """Credentials for the app-user identity (separate from the caller's JWT).

    Used by tools that render or interact with the CUSTOMER'S app as one of
    its end users — screenshot_page, drive_page, call_as_app_user. The
    caller's JWT (developer identity) does ALL platform authoring; these
    credentials only authenticate against the target app's user pool.

    Pass either `token` (pre-obtained) or `username` + `password` (the
    session will run findUserClients + authenticate once and cache the
    resolved token for the conversation's lifetime).
    """

    token: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    app_code: Optional[str] = None
    model: Optional[str] = None
    # Headless/harness callers set this to pre-approve all mutating tools
    # (create/update/delete/copy) so the agent runs fully autonomous without
    # waiting on the interactive confirmation flow. Defaults to the normal
    # interactive behaviour.
    auto_confirm: bool = False
    attachments: Optional[List[ChatAttachment]] = None
    # Optional app-user identity. Required only when a tool that interacts
    # with the customer's live app (screenshot_page / drive_page /
    # call_as_app_user) is invoked. Other tools ignore it.
    app_user: Optional[AppUserAuth] = None


@router.post("/chat")
async def chat(body: ChatRequest, auth: AuthContext = Depends(require_ai_auth_context)):
    """Stream an appbuilder agent response as SSE."""
    if _agent is None:
        raise HTTPException(status_code=503, detail="AppBuilder agent not initialized")

    if body.app_code:
        auth.app_code = body.app_code

    session = BaseSession(agent_name="appbuilder")
    if body.app_code:
        session.context["app_code"] = body.app_code
    # Pre-approve mutating tools for headless/harness callers (see agent loop).
    session.context["auto_confirm"] = body.auto_confirm

    # Stash app-user credentials (token OR username+password) on the session.
    # Consumed lazily by tools that need an end-user identity (screenshot_page,
    # drive_page, call_as_app_user) — other tools ignore it entirely.
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
