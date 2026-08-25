"""Base router — shared session CRUD, models, image handling, and SSE streaming.

Provides a factory that creates common endpoints and reusable helpers.
Agent routers call `create_common_routes()` to get the shared endpoints
and add their own `/chat` on top.

Usage:
    from app.core.base_router import create_common_routes, stream_agent_response

    router = APIRouter()
    create_common_routes(router, agent_name="adzump")

    @router.post("/chat")
    async def chat(...):
        ...
        return stream_agent_response(agent, message, session)
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.base_auth import require_auth_context
from app.core.session import BaseSession, AuthContext
from app.core.streaming import AgentEventStream
from app.db.models import SessionListItem, SessionListResponse, SessionStatus
from app.services.session_manager import get_session_manager
from app.services.context_manager import get_context_manager

logger = logging.getLogger(__name__)


class UpdateSessionRequest(BaseModel):
    """Request body for renaming a session."""
    title: str


class ChatAttachment(BaseModel):
    """An attachment sent with a chat message."""
    type: str = "image"  # "image" or "file"
    name: str = ""
    mime_type: str = "image/png"
    data: Optional[str] = None  # base64-encoded file content


def create_common_routes(router: APIRouter, agent_name: str) -> None:
    """Register shared endpoints on the given router.

    Includes:
        GET /models          — List available LLM models
        GET /sessions        — List sessions (paginated)
        GET /sessions/{id}   — Session detail with conversation history
        PATCH /sessions/{id} — Rename session
        DELETE /sessions/{id} — Delete session

    Args:
        router: The APIRouter to add endpoints to.
        agent_name: Agent name for filtering sessions (e.g. "appbuilder", "adzump").
    """

    @router.get("/models")
    async def list_models(
        auth: AuthContext = Depends(require_auth_context),
    ):
        """List available LLM models."""
        from app.services.llm_provider import get_available_models
        return {"models": get_available_models()}

    @router.get("/sessions")
    async def list_sessions(
        limit: int = 20,
        offset: int = 0,
        status: Optional[str] = None,
        auth: AuthContext = Depends(require_auth_context),
    ):
        """List sessions for the current user."""
        status_filter = SessionStatus(status) if status else None
        session_mgr = get_session_manager()
        sessions, total = await session_mgr.list_sessions(
            user_id=auth.user_id,
            client_code=auth.client_code,
            agent_name=agent_name,
            status=status_filter,
            limit=limit,
            offset=offset,
        )
        items = [
            SessionListItem(
                session_id=s.session_id,
                title=s.title,
                agent_name=s.agent_name,
                app_code=s.app_code,
                status=s.status,
                turn_count=s.turn_count,
                created_at=s.created_at,
                updated_at=s.updated_at,
            )
            for s in sessions
        ]
        return SessionListResponse(items=items, total=total, limit=limit, offset=offset)

    @router.get("/sessions/{session_id}")
    async def get_session(
        session_id: str,
        limit: int = 20,
        offset: int = 0,
        auth: AuthContext = Depends(require_auth_context),
    ):
        """Get session detail with paginated conversation history."""
        session_mgr = get_session_manager()
        session = await session_mgr.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        if session.user_id != auth.user_id:
            raise HTTPException(status_code=403, detail="Access denied")

        context_mgr = get_context_manager()
        history, total = await context_mgr.get_history(
            session_id, limit=limit, offset=offset
        )
        return {
            "session": session,
            "history": [h.model_dump() for h in history],
            "total_history": total,
            "limit": limit,
            "offset": offset,
        }

    @router.patch("/sessions/{session_id}")
    async def update_session(
        session_id: str,
        body: UpdateSessionRequest,
        auth: AuthContext = Depends(require_auth_context),
    ):
        """Rename a session (update title)."""
        session_mgr = get_session_manager()
        session = await session_mgr.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        if session.user_id != auth.user_id:
            raise HTTPException(status_code=403, detail="Access denied")

        success = await session_mgr.update_session_title(
            session_id, body.title, auth.user_id
        )
        if not success:
            raise HTTPException(
                status_code=500, detail="Failed to update session title"
            )
        return {"session_id": session_id, "title": body.title}

    @router.delete("/sessions/{session_id}")
    async def delete_session(
        session_id: str,
        auth: AuthContext = Depends(require_auth_context),
    ):
        """Delete a session and all related data."""
        session_mgr = get_session_manager()
        deleted = await session_mgr.delete_session(session_id, auth.user_id)
        if not deleted:
            raise HTTPException(
                status_code=404, detail="Session not found or access denied"
            )
        return {"deleted": True, "session_id": session_id}


def build_image_blocks(
    attachments: List[ChatAttachment],
    provider_name: str | None = None,
) -> list[dict] | None:
    """Convert chat attachments to image content blocks.

    Images are compressed if they exceed the API size limit.
    """
    from app.services.llm_provider import get_llm_provider
    from app.utils.image import compress_image_base64

    provider = get_llm_provider(provider_name)
    blocks = []
    for i, att in enumerate(attachments):
        if att.data and att.type == "image":
            original_size = len(att.data)
            logger.info("Attachment[%d]: type=%s, mime=%s, base64_size=%d bytes",
                       i, att.type, att.mime_type, original_size)
            data, mime = compress_image_base64(att.data, att.mime_type)
            logger.info("Attachment[%d] after compression: mime=%s, base64_size=%d bytes",
                       i, mime, len(data))
            blocks.append(provider.format_image_content(data, mime))
        else:
            logger.info("Attachment[%d]: type=%s (skipped, not image or no data)", i, att.type)
    return blocks if blocks else None


def sse_stream_response(
    event_stream: AgentEventStream,
    run_coro,
    *,
    keepalive: bool = True,
) -> StreamingResponse:
    """Wrap a run-coroutine + its event stream as an SSE StreamingResponse. Runs a 15s
    keepalive pinger for long agent runs; pass ``keepalive=False`` for a quick one-shot
    stream (e.g. a review-panel widget action that finishes immediately)."""

    async def _keepalive():
        try:
            while True:
                await asyncio.sleep(15)
                await event_stream.emit_keepalive()
        except asyncio.CancelledError:
            pass

    async def _event_generator():
        task = asyncio.create_task(run_coro)
        ka_task = asyncio.create_task(_keepalive()) if keepalive else None
        try:
            async for event in event_stream.events():
                yield event.to_sse()
        except asyncio.CancelledError:
            task.cancel()
            raise
        finally:
            if ka_task is not None:
                ka_task.cancel()
            if not task.done():
                task.cancel()

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def stream_agent_response(
    agent,
    message: str,
    session: BaseSession,
    image_blocks: list[dict] | None = None,
    model_override: str | None = None,
) -> StreamingResponse:
    """Create SSE streaming response for an agent run.

    Works with any agent that implements `run(message, session, event_stream, ...)`.
    """
    event_stream = AgentEventStream()

    async def run_agent():
        try:
            await agent.run(
                message, session, event_stream,
                image_blocks, model_override=model_override,
            )
        except Exception as e:
            logger.exception("Agent run failed")
            await event_stream.emit_error(str(e))
            await event_stream.emit_done(session_id=session.session_id)

    return sse_stream_response(event_stream, run_agent(), keepalive=True)
