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

    forwarded_host, forwarded_port = _extract_forwarded_headers(request)

    # In standalone mode, capture the URL path prefix (e.g. /appbuilder/SYSTEM/page)
    # set by the webpack dev server proxy for outgoing API calls
    from app.config import settings
    path_prefix = ""
    if settings.STANDALONE_MODE:
        path_prefix = request.headers.get("X-Path-Prefix", "")

    referer = request.headers.get("Referer", request.headers.get("Origin", ""))

    return AuthContext(
        token=auth_header,
        client_code=client_code,
        client_id=ctx_auth.user.clientId if ctx_auth.user else 0,
        user_id=ctx_auth.user.id if ctx_auth.user else 0,
        app_code=target_app_code,
        access_app_code=verified_app or access_app_code,
        forwarded_host=forwarded_host,
        forwarded_port=forwarded_port,
        path_prefix=path_prefix,
        referer=referer,
    )


async def _authenticate_session_request(request: Request) -> AuthContext:
    """Lightweight auth for session CRUD endpoints (no target app needed)."""
    auth_header = _extract_token(request)
    client_code = request.headers.get("clientCode", "")
    access_app_code = request.headers.get("appCode", "")

    # In standalone mode, fall back to values from the URL path prefix
    if not client_code or not access_app_code:
        sa_app, sa_client = _extract_standalone_context(request)
        client_code = client_code or sa_client
        access_app_code = access_app_code or sa_app

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

    # In standalone mode, fall back to values from the URL path prefix
    if not client_code or not access_app_code:
        sa_app, sa_client = _extract_standalone_context(request)
        client_code = client_code or sa_client
        access_app_code = access_app_code or sa_app

    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header or token cookie")
    if not client_code:
        raise HTTPException(status_code=400, detail="Missing clientCode header")

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    app_code: Optional[str] = None
    model: Optional[str] = None
    attachments: Optional[List[ChatAttachment]] = None


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

    await session.get_or_create(body.session_id, auth)

    if not body.session_id:
        title = body.message[:100].strip()
        if title:
            await get_session_manager().update_session_title(
                session.session_id, title, auth.user_id
            )

    # Stash the first image attachment's raw base64 in session context so
    # tools (e.g. build_page_from_screenshot) can access the full-resolution
    # image without it being compressed or passed through tool-call JSON.
    if body.attachments:
        for att in body.attachments:
            if att.type == "image" and att.data:
                session.context["user_screenshot_b64"] = att.data
                session.context["user_screenshot_mime"] = att.mime_type
                logger.info("Stashed user screenshot in session context (%d bytes)", len(att.data))
                break

    # Convert attachments to image content blocks using the agent's provider.
    # If the configured provider can't process images (DeepSeek's hosted API
    # is text-only), fall back to any vision-capable provider we have a key for.
    image_blocks = None
    effective_model = body.model
    if body.attachments:
        from app.services.llm_provider import get_llm_provider
        provider_name = _agent._provider_name if _agent else None
        provider = get_llm_provider(provider_name)

        if not provider.supports_vision():
            from app.config import settings
            fallback_name = (
                "openai" if settings.OPENAI_API_KEY
                else "anthropic" if settings.ANTHROPIC_API_KEY
                else None
            )
            if fallback_name and fallback_name != provider_name:
                logger.info(
                    "Provider %s has no vision — using %s for this turn.",
                    provider_name or "(default)", fallback_name,
                )
                provider_name = fallback_name
                effective_model = effective_model or f"{fallback_name}:balanced"
            else:
                logger.warning(
                    "Provider %s has no vision and no vision-capable fallback "
                    "is configured; image will be dropped.",
                    provider_name or "(default)",
                )

        logger.info(
            "Processing %d attachment(s) with provider=%s model=%s",
            len(body.attachments),
            provider_name or "(default)",
            effective_model or "(default-tier)",
        )
        image_blocks = _build_image_blocks(body.attachments, provider_name)
        logger.info("Image blocks built: %d", len(image_blocks) if image_blocks else 0)

    return _stream_agent_response(body.message, session, image_blocks, effective_model)


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
    sid = session.session_id

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
        _active_streams[sid] = event_stream
        task = asyncio.create_task(run_agent())
        keepalive_task = asyncio.create_task(keepalive())
        try:
            async for event in event_stream.events():
                yield event.to_sse()
        except (asyncio.CancelledError, GeneratorExit):
            # Client disconnected — signal the agent to stop gracefully
            event_stream.cancel()
            task.cancel()
        finally:
            keepalive_task.cancel()
            # If client disconnected but agent is still running, cancel it
            if not task.done():
                event_stream.cancel()
                task.cancel()
            # Clean up after agent finishes (give it a moment to wrap up)
            _active_streams.pop(sid, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/confirm")
async def confirm_action(request: Request, body: ConfirmationResponse):
    """Resolve a pending confirmation request from the agent."""
    await _authenticate_session_request(request)

    event_stream = _active_streams.get(body.session_id)
    if not event_stream:
        raise HTTPException(status_code=404, detail="No active stream for this session")

    resolved = event_stream.resolve_confirmation(
        body.confirmation_id,
        {
            "approved": body.approved,
            "selected": body.selected or ("approve" if body.approved else "deny"),
            "reason": body.reason,
        },
    )
    if not resolved:
        raise HTTPException(status_code=404, detail="Confirmation not found or already resolved")

    return {"resolved": True, "confirmation_id": body.confirmation_id}


class StopRequest(BaseModel):
    """Request body for stopping an active agent run."""
    session_id: str


@router.post("/stop")
async def stop_agent(request: Request, body: StopRequest):
    """Stop an active agent run for the given session."""
    await _authenticate_session_request(request)

    event_stream = _active_streams.get(body.session_id)
    if event_stream:
        event_stream.cancel()

    # Always mark the session as completed so polling stops on refresh
    try:
        session_mgr = get_session_manager()
        await session_mgr.complete_session(body.session_id)
    except Exception:
        pass

    return {"stopped": True, "session_id": body.session_id}


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
