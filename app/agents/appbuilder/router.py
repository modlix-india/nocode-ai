"""AppBuilder router — chat + session management endpoints.

Endpoints:
    POST /chat               — Stream agent response as SSE
    GET  /sessions            — List sessions (paginated)
    GET  /sessions/{id}       — Session detail with conversation history
    PATCH /sessions/{id}      — Rename session (update title)
    DELETE /sessions/{id}     — Delete session and related data
"""

from __future__ import annotations

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

# Module-level reference to the agent (set during startup)
_agent = None


def set_appbuilder_agent(agent) -> None:
    """Set the AppBuilderAgent instance (called from main.py lifespan)."""
    global _agent
    _agent = agent


class ChatAttachment(BaseModel):
    """An attachment sent with a chat message."""
    type: str = "image"  # "image" or "file"
    name: str = ""
    mime_type: str = "image/png"
    data: Optional[str] = None  # base64-encoded file content


class ChatRequest(BaseModel):
    """Request body for the chat endpoint."""
    message: str
    session_id: Optional[str] = None
    app_code: Optional[str] = None
    model: Optional[str] = None  # Model override: "provider:model_name"
    attachments: Optional[List[ChatAttachment]] = None


class UpdateSessionRequest(BaseModel):
    """Request body for renaming a session."""
    title: str


def _extract_token(request: Request) -> str:
    """Extract auth token from Authorization header or cookie fallback."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header:
        return auth_header
    cookie_token = request.cookies.get("Authorization", "")
    if cookie_token:
        return f"Bearer {cookie_token}"
    return ""


def _extract_forwarded_headers(request: Request) -> tuple[str, str]:
    """Extract X-Forwarded-Host/Port from request (set by gateway/proxy)."""
    host = request.headers.get(
        "X-Forwarded-Host", request.url.hostname or "localhost"
    )
    port = request.headers.get(
        "X-Forwarded-Port", str(request.url.port or 80)
    )
    # Handle comma-separated port (matching Java behavior)
    if "," in port:
        port = port.split(",")[0]
    return host, port


async def _authenticate(
    request: Request,
    auth_header: str,
    client_code: str,
    access_app_code: str,
    target_app_code: str,
) -> AuthContext:
    """Validate token and verify AI access.

    Args:
        access_app_code: The app from the header (must be appbuilder/sitezump).
        target_app_code: The app to build/edit (from request body).

    Raises HTTPException on auth failure or access denial.
    """
    from app.services.security import get_context_authentication, ALLOWED_AI_APPS

    ctx_auth = await get_context_authentication(
        request=request,
        authorization=auth_header,
        client_code=client_code,
        app_code=access_app_code,
    )
    if not ctx_auth.isAuthenticated:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Verify the access app is an AI-enabled app
    verified_app = ctx_auth.verifiedAppCode or access_app_code
    if not verified_app or verified_app.lower() not in ALLOWED_AI_APPS:
        raise HTTPException(
            status_code=403,
            detail="AI features are only available in appbuilder or sitezump applications.",
        )

    forwarded_host, forwarded_port = _extract_forwarded_headers(request)

    return AuthContext(
        token=auth_header,
        client_code=client_code,
        client_id=ctx_auth.user.clientId if ctx_auth.user else 0,
        user_id=ctx_auth.user.id if ctx_auth.user else 0,
        app_code=target_app_code,
        access_app_code=verified_app or access_app_code,
        forwarded_host=forwarded_host,
        forwarded_port=forwarded_port,
    )


async def _authenticate_session_request(request: Request) -> AuthContext:
    """Lightweight auth for session CRUD endpoints (no target app needed)."""
    auth_header = _extract_token(request)
    client_code = request.headers.get("clientCode", "")
    access_app_code = request.headers.get("appCode", "")

    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header or token cookie")
    if not client_code:
        raise HTTPException(status_code=400, detail="Missing clientCode header")

    return await _authenticate(request, auth_header, client_code, access_app_code, "")


async def _authenticate_chat_request(request: Request, body: ChatRequest) -> AuthContext:
    """Authenticate and build AuthContext for chat requests."""
    auth_header = _extract_token(request)
    client_code = request.headers.get("clientCode", "")
    access_app_code = request.headers.get("appCode", "")
    target_app_code = body.app_code or ""

    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header or token cookie")
    if not client_code:
        raise HTTPException(status_code=400, detail="Missing clientCode header")

    try:
        return await _authenticate(request, auth_header, client_code, access_app_code, target_app_code)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token validation failed: {e}")
        raise HTTPException(status_code=401, detail="Token validation failed")


@router.get("/models")
async def list_models(request: Request):
    """List available LLM models for the appbuilder agent."""
    await _authenticate_session_request(request)

    from app.services.llm_provider import get_available_models
    return {"models": get_available_models()}


@router.post("/chat")
async def chat(request: Request, body: ChatRequest):
    """Stream an appbuilder agent response as SSE."""
    if _agent is None:
        logger.error("Chat request rejected: AppBuilder agent is None (not initialized)")
        raise HTTPException(status_code=503, detail="AppBuilder agent not initialized")

    logger.info("Chat request: session=%s, app=%s, model=%s, attachments=%d, message_len=%d",
                body.session_id or "(new)", body.app_code or "(none)",
                body.model or "(default)",
                len(body.attachments) if body.attachments else 0, len(body.message))

    auth = await _authenticate_chat_request(request, body)
    logger.info("Authenticated: user=%d, client=%s", auth.user_id, auth.client_code)

    # Create/resume session
    session = BaseSession(agent_name="appbuilder")

    # Seed context from request before session load (so it takes precedence on merge)
    if body.app_code:
        session.context["app_code"] = body.app_code

    await session.get_or_create(body.session_id, auth)
    logger.info("Session ready: %s", session.session_id)

    # Auto-set title on new sessions from first message
    if not body.session_id:
        title = body.message[:100].strip()
        if title:
            await get_session_manager().update_session_title(
                session.session_id, title, auth.user_id
            )

    # Convert attachments to image content blocks using the agent's provider
    image_blocks = None
    if body.attachments:
        provider_name = _agent._provider_name if _agent else None
        logger.info("Processing %d attachment(s) with provider=%s",
                    len(body.attachments), provider_name or "(default)")
        image_blocks = _build_image_blocks(body.attachments, provider_name)
        logger.info("Image blocks built: %d", len(image_blocks) if image_blocks else 0)

    return _stream_agent_response(body.message, session, image_blocks, body.model)


def _build_image_blocks(attachments: List[ChatAttachment], provider_name: str | None = None) -> list[dict] | None:
    """Convert chat attachments to image content blocks using the agent's provider.

    Images are compressed if they exceed the API size limit (5MB for Anthropic).
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


def _stream_agent_response(
    message: str,
    session: BaseSession,
    image_blocks: list[dict] | None = None,
    model_override: str | None = None,
) -> StreamingResponse:
    """Create SSE streaming response for an agent run."""
    import asyncio

    event_stream = AgentEventStream()

    async def run_agent():
        try:
            await _agent.run(message, session, event_stream, image_blocks, model_override)
        except Exception as e:
            logger.exception("Agent run failed")
            await event_stream.emit_error(str(e))
            await event_stream.emit_done(session_id=session.session_id)

    async def keepalive():
        """Send keepalive pings every 15s to prevent connection timeout."""
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
async def list_sessions(
    request: Request,
    limit: int = 20,
    offset: int = 0,
    status: Optional[str] = None,
):
    """List sessions for the current user (appbuilder agent)."""
    auth = await _authenticate_session_request(request)

    status_filter = SessionStatus(status) if status else None
    session_mgr = get_session_manager()
    sessions, total = await session_mgr.list_sessions(
        user_id=auth.user_id,
        client_code=auth.client_code,
        agent_name="appbuilder",
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
    request: Request,
    session_id: str,
    limit: int = 20,
    offset: int = 0,
):
    """Get session detail with paginated conversation history."""
    auth = await _authenticate_session_request(request)

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
async def update_session(request: Request, session_id: str, body: UpdateSessionRequest):
    """Rename a session (update title)."""
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
    """Delete a session and all related data."""
    auth = await _authenticate_session_request(request)

    session_mgr = get_session_manager()
    deleted = await session_mgr.delete_session(session_id, auth.user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found or access denied")

    return {"deleted": True, "session_id": session_id}
