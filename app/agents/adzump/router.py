"""Adzump router — chat + session management endpoints.

Endpoints:
    POST /chat               — Stream agent response as SSE
    GET  /sessions            — List sessions (paginated)
    GET  /sessions/{id}       — Session detail with conversation history
    PATCH /sessions/{id}      — Rename session (update title)
    DELETE /sessions/{id}     — Delete session and related data
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, List

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.streaming import AgentEventStream
from app.core.session import BaseSession, AuthContext
from app.db.models import SessionListItem, SessionListResponse, SessionStatus
from app.services.session_manager import get_session_manager
from app.services.context_manager import get_context_manager

logger = logging.getLogger(__name__)

router = APIRouter()

from app.agents.adzump.chat_agent import AdzumpChatAgent


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    model: Optional[str] = None


class UpdateSessionRequest(BaseModel):
    title: str


def _extract_token(request: Request) -> str:
    auth_header = request.headers.get("Authorization", "")
    if auth_header:
        return auth_header
    cookie_token = request.cookies.get("Authorization", "")
    if cookie_token:
        return f"Bearer {cookie_token}"
    return ""


def _extract_forwarded_headers(request: Request) -> tuple[str, str]:
    host = request.headers.get("X-Forwarded-Host", request.url.hostname or "localhost")
    port = request.headers.get("X-Forwarded-Port", str(request.url.port or 80))
    if "," in port:
        port = port.split(",")[0]
    return host, port


async def _authenticate(request: Request, auth_header: str, client_code: str, access_app_code: str) -> AuthContext:
    from app.services.security import get_context_authentication

    ctx_auth = await get_context_authentication(
        request=request,
        authorization=auth_header,
        client_code=client_code,
        app_code=access_app_code,
    )
    if not ctx_auth.isAuthenticated:
        raise HTTPException(status_code=401, detail="Not authenticated")

    forwarded_host, forwarded_port = _extract_forwarded_headers(request)

    return AuthContext(
        token=auth_header,
        client_code=client_code,
        client_id=ctx_auth.user.clientId if ctx_auth.user else 0,
        user_id=ctx_auth.user.id if ctx_auth.user else 0,
        app_code=access_app_code,
        access_app_code=access_app_code,
        forwarded_host=forwarded_host,
        forwarded_port=forwarded_port,
    )


async def _authenticate_session_request(request: Request) -> AuthContext:
    auth_header = _extract_token(request)
    client_code = request.headers.get("clientCode", "")
    access_app_code = request.headers.get("appCode", "")

    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header or token cookie")
    if not client_code:
        raise HTTPException(status_code=400, detail="Missing clientCode header")

    return await _authenticate(request, auth_header, client_code, access_app_code)


async def _authenticate_chat_request(request: Request) -> AuthContext:
    auth_header = _extract_token(request)
    client_code = request.headers.get("clientCode", "")
    access_app_code = request.headers.get("appCode", "")

    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header or token cookie")
    if not client_code:
        raise HTTPException(status_code=400, detail="Missing clientCode header")

    try:
        return await _authenticate(request, auth_header, client_code, access_app_code)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token validation failed: {e}")
        raise HTTPException(status_code=401, detail="Token validation failed")


@router.post("/chat")
async def chat(request: Request, body: ChatRequest):
    """Stream an adzump agent response as SSE."""
    agent = AdzumpChatAgent.get_instance()

    logger.info("Adzump chat: session=%s, model=%s, message_len=%d",
                body.session_id or "(new)", body.model or "(default)", len(body.message))

    auth = await _authenticate_chat_request(request)

    session = BaseSession(agent_name="adzump")
    await session.get_or_create(body.session_id, auth)
    logger.info("Session ready: %s", session.session_id)

    if not body.session_id:
        title = body.message[:100].strip()
        if title:
            await get_session_manager().update_session_title(
                session.session_id, title, auth.user_id
            )

    return _stream_agent_response(agent, body.message, session, body.model)


def _stream_agent_response(
    agent: AdzumpChatAgent,
    message: str,
    session: BaseSession,
    model_override: str | None = None,
) -> StreamingResponse:
    event_stream = AgentEventStream()

    async def run_agent():
        try:
            await agent.run(message, session, event_stream, model_override=model_override)
        except Exception as e:
            logger.exception("Agent run failed")
            await event_stream.emit_error(str(e))
            await event_stream.emit_done(session_id=session.session_id)

    async def keepalive():
        try:
            while True:
                await asyncio.sleep(15)
                await event_stream.emit_keepalive()
        except asyncio.CancelledError:
            pass

    async def event_generator():
        task = asyncio.create_task(run_agent())
        keepalive_task = asyncio.create_task(keepalive())
        try:
            async for event in event_stream.events():
                yield event.to_sse()
        except asyncio.CancelledError:
            task.cancel()
            raise
        finally:
            keepalive_task.cancel()
            if not task.done():
                task.cancel()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Session management endpoints ─────────────────────────────────


@router.get("/sessions")
async def list_sessions(request: Request, limit: int = 20, offset: int = 0, status: Optional[str] = None):
    auth = await _authenticate_session_request(request)
    status_filter = SessionStatus(status) if status else None
    session_mgr = get_session_manager()
    sessions, total = await session_mgr.list_sessions(
        user_id=auth.user_id,
        client_code=auth.client_code,
        agent_name="adzump",
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    items = [
        SessionListItem(
            session_id=s.session_id, title=s.title, agent_name=s.agent_name,
            app_code=s.app_code, status=s.status, turn_count=s.turn_count,
            created_at=s.created_at, updated_at=s.updated_at,
        )
        for s in sessions
    ]
    return SessionListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/sessions/{session_id}")
async def get_session(request: Request, session_id: str, limit: int = 20, offset: int = 0):
    auth = await _authenticate_session_request(request)
    session_mgr = get_session_manager()
    session = await session_mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.user_id != auth.user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    context_mgr = get_context_manager()
    history, total = await context_mgr.get_history(session_id, limit=limit, offset=offset)
    return {
        "session": session,
        "history": [h.model_dump() for h in history],
        "total_history": total,
        "limit": limit,
        "offset": offset,
    }


@router.patch("/sessions/{session_id}")
async def update_session(request: Request, session_id: str, body: UpdateSessionRequest):
    auth = await _authenticate_session_request(request)
    session_mgr = get_session_manager()
    session = await session_mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.user_id != auth.user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    success = await session_mgr.update_session_title(session_id, body.title, auth.user_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to update session title")
    return {"session_id": session_id, "title": body.title}


@router.delete("/sessions/{session_id}")
async def delete_session(request: Request, session_id: str):
    auth = await _authenticate_session_request(request)
    session_mgr = get_session_manager()
    deleted = await session_mgr.delete_session(session_id, auth.user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found or access denied")
    return {"deleted": True, "session_id": session_id}
