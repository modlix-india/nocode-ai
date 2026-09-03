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

import logging
from typing import AsyncIterator, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.base_auth import require_auth_context
from app.core.session import BaseSession, AuthContext
from app.core.streaming import AgentEvent
from app.core import run_manager, stream_registry
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


class StopRequest(BaseModel):
    """Request body for interrupting a running agent."""
    session_id: str


class ConfirmRequest(BaseModel):
    """Request body for answering a tool confirmation the agent is blocked on."""
    session_id: str
    confirmation_id: str
    approved: bool = False
    selected: Optional[str] = None


class AttachRequest(BaseModel):
    """Request body for rejoining a run already in progress."""
    session_id: str


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
        app_code: Optional[str] = None,
        auth: AuthContext = Depends(require_auth_context),
    ):
        """List sessions for the current user.

        `app_code` narrows the list to chats started against one app. Callers
        embedded in a per-app surface (the appbuilder workspace sidekick) pass
        it so each workspace has its own history rather than one shared pile.
        """
        status_filter = SessionStatus(status) if status else None
        session_mgr = get_session_manager()
        sessions, total = await session_mgr.list_sessions(
            user_id=auth.user_id,
            client_code=auth.client_code,
            agent_name=agent_name,
            status=status_filter,
            app_code=app_code,
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

    @router.post("/stop")
    async def stop_session(
        body: StopRequest,
        auth: AuthContext = Depends(require_auth_context),
    ):
        """Ask the agent serving this session to stop at its next checkpoint.

        The stream keeps running until the loop notices, so this returns as soon
        as the signal is delivered rather than waiting for the run to unwind.
        """
        await _assert_session_owner(body.session_id, auth)
        delivered = await stream_registry.signal(body.session_id, "stop")
        if delivered == "missing":
            raise HTTPException(status_code=404, detail="No run in progress for this session")
        return {
            "stopped": delivered == "local",
            "delivery": delivered,
            "session_id": body.session_id,
        }

    @router.get("/runs")
    async def list_runs(
        auth: AuthContext = Depends(require_auth_context),
    ):
        """Which of this user's sessions have an agent still working.

        Asked on load, so a client that was disconnected (a refresh, a closed
        panel, a switch to another session) can rejoin the turn instead of
        showing a finished-looking chat that is still being written.
        """
        runs = await run_manager.list_live_runs(auth.user_id, agent_name=agent_name)
        return {"runs": runs}

    @router.post("/attach")
    async def attach_run(
        body: AttachRequest,
        auth: AuthContext = Depends(require_auth_context),
    ):
        """Rejoin a run in progress: replays the turn so far, then streams live.

        The whole turn is replayed every time rather than resumed from a
        cursor, and the client rebuilds the message from what arrives. Text
        deltas are coalesced in the buffer, so there is no stable cursor into
        them to resume from. See `run_manager`.
        """
        await _assert_session_owner(body.session_id, auth)
        events = await run_manager.subscribe(body.session_id)
        if events is None:
            # Nothing to rejoin: it finished long enough ago to have been
            # forgotten, or its worker died. The client falls back to history.
            raise HTTPException(
                status_code=404, detail="No run to attach to for this session"
            )
        return sse_response(events)

    @router.post("/confirm")
    async def confirm_tool(
        body: ConfirmRequest,
        auth: AuthContext = Depends(require_auth_context),
    ):
        """Answer a confirmation the agent is blocked on.

        Without this the agent waits out its 120s timeout and then denies
        itself, so every mutating tool call fails by default.
        """
        await _assert_session_owner(body.session_id, auth)
        delivered = await stream_registry.signal(
            body.session_id,
            "confirm",
            {
                "confirmation_id": body.confirmation_id,
                "approved": body.approved,
                "selected": body.selected,
            },
        )
        if delivered == "missing":
            # Nothing was waiting: the agent already timed out, or the run ended.
            raise HTTPException(
                status_code=404, detail="No pending confirmation for this session"
            )
        return {
            # Only a local hit proves a waiting confirmation actually took this.
            "resolved": delivered == "local",
            "delivery": delivered,
            "session_id": body.session_id,
        }


async def _assert_session_owner(session_id: str, auth: AuthContext) -> None:
    """Refuse to signal a session the caller does not own.

    A session id is guessable enough that skipping this would let anyone
    interrupt, or silently approve writes in, someone else's agent run.
    """
    session = await get_session_manager().get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.user_id != auth.user_id:
        raise HTTPException(status_code=403, detail="Access denied")


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


def sse_response(events: AsyncIterator[AgentEvent]) -> StreamingResponse:
    """Wrap a stream of agent events as an SSE response.

    Losing this response does NOT end the run behind it: the generator only
    unsubscribes (`run_manager.AgentRun.subscribe` cleans up in its own
    finally), and the agent goes on working for whoever attaches next. That is
    the difference between closing a panel and pressing Stop, and it used to be
    lost: a disconnect cancelled the agent task mid-tool and the turn was
    written to history as "[Stopped by user]".
    """

    async def event_generator():
        try:
            async for event in events:
                yield event.to_sse()
        finally:
            # Close the subscription explicitly. A disconnect cancels this
            # generator and leaves the one underneath suspended at its yield,
            # so waiting for the garbage collector to run its cleanup would
            # leave the run fanning events out to a queue nobody reads.
            # Unsubscribing is synchronous, so this cannot be interrupted by
            # the cancellation already in flight.
            await events.aclose()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def stream_agent_response(
    agent,
    message: str,
    session: BaseSession,
    image_blocks: list[dict] | None = None,
    model_override: str | None = None,
) -> StreamingResponse:
    """Start a detached agent run and stream it back.

    Works with any agent that implements `run(message, session, event_stream, ...)`.

    Raises:
        HTTPException 409: a run is already in flight for this session. The
            client is expected to POST /attach to that run instead of starting
            a second one, because two agents interleaving tool calls and history
            writes on one session corrupts both.
    """
    try:
        run = await run_manager.start_run(
            agent, message, session, image_blocks, model_override,
        )
    except run_manager.RunAlreadyActive as e:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "A run is already in progress for this session",
                "session_id": e.session_id,
                "run_id": e.run_id,
            },
        ) from e

    return sse_response(run.subscribe())
