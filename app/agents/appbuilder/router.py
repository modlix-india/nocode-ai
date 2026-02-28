"""AppBuilder router — POST /api/ai/appbuilder/chat SSE endpoint.

Handles authentication, creates/resumes sessions, and streams
agent responses as Server-Sent Events.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.streaming import AgentEventStream
from app.core.session import BaseSession, AuthContext

logger = logging.getLogger(__name__)

router = APIRouter()

# Module-level reference to the agent (set during startup)
_agent = None


def set_appbuilder_agent(agent) -> None:
    """Set the AppBuilderAgent instance (called from main.py lifespan)."""
    global _agent
    _agent = agent


class ChatRequest(BaseModel):
    """Request body for the chat endpoint."""
    message: str
    session_id: Optional[str] = None
    app_code: Optional[str] = None


def _extract_token(request: Request) -> str:
    """Extract auth token from Authorization header or cookie fallback."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header:
        return auth_header
    cookie_token = request.cookies.get("token", "")
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


async def _authenticate(request: Request, auth_header: str, client_code: str, app_code: str) -> AuthContext:
    """Validate token via security service and build AuthContext.

    Raises HTTPException on auth failure.
    """
    from app.services.security import get_context_authentication

    ctx_auth = await get_context_authentication(
        request=request,
        authorization=auth_header,
        client_code=client_code,
        app_code=app_code,
    )
    if not ctx_auth.isAuthenticated:
        raise HTTPException(status_code=401, detail="Not authenticated")

    forwarded_host, forwarded_port = _extract_forwarded_headers(request)

    return AuthContext(
        token=auth_header,
        client_code=client_code,
        client_id=ctx_auth.user.clientId if ctx_auth.user else 0,
        user_id=ctx_auth.user.id if ctx_auth.user else 0,
        app_code=ctx_auth.verifiedAppCode or app_code,
        forwarded_host=forwarded_host,
        forwarded_port=forwarded_port,
    )


@router.post("/chat")
async def chat(request: Request, body: ChatRequest):
    """Stream an appbuilder agent response as SSE.

    Authenticates via Bearer token (or cookie fallback),
    creates/resumes a session, and streams agent responses as SSE.
    """
    if _agent is None:
        raise HTTPException(status_code=503, detail="AppBuilder agent not initialized")

    auth_header = _extract_token(request)
    client_code = request.headers.get("clientCode", "")
    app_code = body.app_code or request.headers.get("appCode", "")

    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header or token cookie")
    if not client_code:
        raise HTTPException(status_code=400, detail="Missing clientCode header")

    try:
        auth = await _authenticate(request, auth_header, client_code, app_code)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token validation failed: {e}")
        raise HTTPException(status_code=401, detail="Token validation failed")

    # Create/resume session
    session = BaseSession(agent_name="appbuilder")
    await session.get_or_create(body.session_id, auth)

    # Create event stream
    event_stream = AgentEventStream()

    # Run agent in background task, stream events to response
    import asyncio

    async def run_agent():
        try:
            await _agent.run(body.message, session, event_stream)
        except Exception as e:
            logger.exception("Agent run failed")
            await event_stream.emit_error(str(e))
            await event_stream.emit_done(session_id=session.session_id)

    async def event_generator():
        # Start agent in background
        task = asyncio.create_task(run_agent())

        try:
            async for event in event_stream.events():
                yield event.to_sse()
        except asyncio.CancelledError:
            task.cancel()
            raise
        finally:
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
