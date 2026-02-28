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


@router.post("/chat")
async def chat(request: Request, body: ChatRequest):
    """Stream an appbuilder agent response as SSE.

    Authenticates via Bearer token, creates/resumes a session,
    runs the agentic loop, and streams events to the client.
    """
    if _agent is None:
        raise HTTPException(status_code=503, detail="AppBuilder agent not initialized")

    # Extract auth from headers
    auth_header = request.headers.get("Authorization", "")
    client_code = request.headers.get("clientCode", "")
    app_code = body.app_code or request.headers.get("appCode", "")

    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    if not client_code:
        raise HTTPException(status_code=400, detail="Missing clientCode header")

    # Validate token via security service
    try:
        from app.services.security import validate_token
        user_info = await validate_token(auth_header)
        if not user_info:
            raise HTTPException(status_code=401, detail="Invalid token")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token validation failed: {e}")
        raise HTTPException(status_code=401, detail="Token validation failed")

    auth = AuthContext(
        token=auth_header,
        client_code=client_code,
        client_id=user_info.get("clientId", 0),
        user_id=user_info.get("userId", 0),
        app_code=app_code,
    )

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
