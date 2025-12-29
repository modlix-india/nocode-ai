"""Agent API routes with SSE streaming"""
from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse, ServerSentEvent
import asyncio
import json
from typing import AsyncGenerator, Union

from app.agents.page_agent import (
    PageAgent,
    PageAgentRequest,
    PageAgentResponse
)
from app.services.security import require_ai_access
from app.api.models.auth import ContextAuthentication
from app.streaming.events import ProgressCallback, EventType

router = APIRouter()

# Global page agent instance (initialized in main.py lifespan)
_page_agent: PageAgent = None


def get_page_agent() -> PageAgent:
    """Get the page agent instance"""
    global _page_agent
    if _page_agent is None:
        _page_agent = PageAgent()
    return _page_agent


def set_page_agent(agent: PageAgent):
    """Set the page agent instance (called from main.py)"""
    global _page_agent
    _page_agent = agent


async def stream_page_generation(
    request: PageAgentRequest,
    page_agent: PageAgent
) -> AsyncGenerator[Union[ServerSentEvent, dict], None]:
    """Generate SSE events for page generation progress"""
    import logging
    logger = logging.getLogger(__name__)
    
    progress = ProgressCallback()
    
    async def run_generation():
        """Run generation in background"""
        try:
            logger.info("Starting page generation...")
            result = await page_agent.execute(request, progress_callback=progress)
            logger.info("Page generation complete, sending result...")
            await progress.complete(result.model_dump())
        except Exception as e:
            logger.error(f"Page generation error: {e}", exc_info=True)
            await progress.error(str(e))
    
    # Start generation task
    task = asyncio.create_task(run_generation())
    
    # Stream events
    try:
        while True:
            try:
                event = await asyncio.wait_for(progress.queue.get(), timeout=120.0)
                logger.info(f"Sending SSE event: {event.event.value} - {event.message[:50] if event.message else ''}")
                
                # Format as ServerSentEvent for sse_starlette
                if event.event == EventType.KEEPALIVE:
                    # Keepalive as comment
                    yield ServerSentEvent(comment=f"keepalive {event.message}")
                else:
                    # Build event data
                    event_data = {
                        "agent": event.agent,
                        "message": event.message,
                    }
                    if event.data:
                        event_data["data"] = event.data
                    
                    yield ServerSentEvent(
                        event=event.event.value,
                        data=json.dumps(event_data)
                    )
                
                if event.event in [EventType.COMPLETE, EventType.ERROR]:
                    logger.info(f"Stream ending with: {event.event.value}")
                    break
                    
            except asyncio.TimeoutError:
                logger.debug("Sending keepalive ping")
                # Send keepalive comment
                yield ServerSentEvent(comment="ping")
                
    finally:
        progress.close()
        if not task.done():
            task.cancel()


@router.post("/page")
async def generate_page_streaming(
    request: PageAgentRequest,
    auth: ContextAuthentication = Depends(require_ai_access)
):
    """
    Generate or modify a page with SSE streaming progress.
    
    Returns a Server-Sent Events stream with real-time progress updates:
    
    **Event Types:**
    - `status`: Overall status messages
    - `phase`: Phase transitions (Foundation, Enhancement, Review)
    - `agent_start`: Sub-agent started working
    - `agent_thinking`: Sub-agent reasoning/progress
    - `agent_complete`: Sub-agent finished
    - `merging`: Merging agent outputs
    - `complete`: Final result with page JSON
    - `error`: Error occurred
    
    **Request Modes:**
    - `create`: Generate new page from instruction
    - `modify`: Modify existing page based on instruction
    - `enhance`: Add features to existing page
    
    **Example:**
    ```
    POST /api/ai/agent/page
    Content-Type: application/json
    Authorization: Bearer <token>
    
    {
      "instruction": "Create a login page with email and password",
      "options": { "mode": "create" }
    }
    ```
    
    **SSE Response:**
    ```
    event: status
    data: {"message": "Starting page generation..."}
    
    event: agent_start
    data: {"agent": "Layout", "message": "Analyzing layout requirements..."}
    
    event: complete
    data: {"success": true, "page": {...}, "agentLogs": {...}}
    ```
    """
    page_agent = get_page_agent()
    
    return EventSourceResponse(
        stream_page_generation(request, page_agent),
        media_type="text/event-stream",
        headers={
            # Disable nginx buffering for real-time streaming
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache, no-store",
            "Connection": "keep-alive",
        }
    )


@router.post("/page/sync", response_model=PageAgentResponse)
async def generate_page_sync(
    request: PageAgentRequest,
    auth: ContextAuthentication = Depends(require_ai_access)
) -> PageAgentResponse:
    """
    Generate or modify a page (synchronous - waits for complete result).
    
    Use `POST /agent/page` for streaming progress updates.
    
    **Request Modes:**
    - `create`: Generate new page from instruction
    - `modify`: Modify existing page based on instruction
    - `enhance`: Add features to existing page
    """
    try:
        page_agent = get_page_agent()
        result = await page_agent.execute(request)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

