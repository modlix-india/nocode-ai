"""Agent API routes with SSE streaming"""
from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel
import asyncio
import json
from typing import AsyncGenerator, Union, Optional

from app.agents.page_agent import (
    PageAgent,
    PageAgentRequest,
    PageAgentResponse,
    PageAgentMode,
    PageAgentOptions
)
from app.services.security import require_ai_access
from app.api.models.auth import ContextAuthentication
from app.streaming.events import ProgressCallback, EventType


class WebsiteImportRequest(BaseModel):
    """Request to import a website and convert to Nocode page"""
    sourceUrl: str  # URL of the website to import
    clientCode: Optional[str] = None  # Client code for image uploads (usually from auth)


class WebsiteImportResponse(BaseModel):
    """Response from website import"""
    success: bool
    page: dict  # The generated page definition
    stats: dict  # Statistics about the import (elements, components, images)
    errors: list = []  # Any errors encountered

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
    page_agent: PageAgent,
    client_code: str = None,
    auth_context: dict = None
) -> AsyncGenerator[Union[ServerSentEvent, dict], None]:
    """Generate SSE events for page generation progress"""
    import logging
    logger = logging.getLogger(__name__)

    # Inject clientCode into request if provided from auth
    if client_code and not request.clientCode:
        request.clientCode = client_code

    progress = ProgressCallback()

    async def run_generation():
        """Run generation in background"""
        try:
            logger.info("Starting page generation...")
            result = await page_agent.execute(
                request,
                progress_callback=progress,
                auth_context=auth_context
            )
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
    Generate, modify, or import a page with SSE streaming progress.
    
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
    - `import`: Import and convert from external website URL
    
    **Example - Create Page:**
    ```
    POST /api/ai/agent/page
    Content-Type: application/json
    Authorization: Bearer <token>
    
    {
      "instruction": "Create a login page with email and password",
      "options": { "mode": "create" }
    }
    ```
    
    **Example - Import from Website:**
    ```
    POST /api/ai/agent/page
    Content-Type: application/json
    Authorization: Bearer <token>
    
    {
      "instruction": "Import this landing page",
      "sourceUrl": "https://example.com/landing",
      "options": { "mode": "import" }
    }
    ```
    
    **Import Mode Details:**
    - Fetches HTML and takes screenshot of the target URL
    - Analyzes page structure, styles, and layout
    - Creates placeholder Image components (empty src) for all images
    - Generates appropriate animations based on the original site
    - Images are NOT downloaded; use empty placeholders for later replacement
    
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

    # Build auth context for session tracking
    auth_context = None
    client_code = None
    if auth:
        client_code = auth.clientCode
        auth_context = {
            "clientCode": auth.clientCode or (auth.user.clientCode if auth.user else ""),
            "clientId": auth.user.clientId if auth.user else 0,
            "userId": auth.user.id if auth.user else 0,
            "appCode": auth.verifiedAppCode or auth.urlAppCode
        }

    return EventSourceResponse(
        stream_page_generation(
            request,
            page_agent,
            client_code=client_code,
            auth_context=auth_context
        ),
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
    Generate, modify, or import a page (synchronous - waits for complete result).
    
    Use `POST /agent/page` for streaming progress updates.
    
    **Request Modes:**
    - `create`: Generate new page from instruction
    - `modify`: Modify existing page based on instruction
    - `enhance`: Add features to existing page
    - `import`: Import and convert from external website URL (requires `sourceUrl`)
    
    **Import Mode:**
    Set `sourceUrl` to import from an external website. The website will be
    fetched, analyzed, and converted to Nocode page format. Images are downloaded
    and uploaded to the files service using the client's storage.

    **Session Tracking:**
    The response includes `sessionId` and `tokenUsage` for tracking token consumption
    across multiple requests. Pass the `sessionId` in subsequent requests to continue
    the same session and enable multi-turn context.
    """
    try:
        # Inject clientCode from auth if not provided
        if auth and auth.clientCode and not request.clientCode:
            request.clientCode = auth.clientCode

        # Build auth context for session tracking
        auth_context = None
        if auth:
            auth_context = {
                "clientCode": auth.clientCode or (auth.user.clientCode if auth.user else ""),
                "clientId": auth.user.clientId if auth.user else 0,
                "userId": auth.user.id if auth.user else 0,
                "appCode": auth.verifiedAppCode or auth.urlAppCode
            }

        page_agent = get_page_agent()
        result = await page_agent.execute(request, auth_context=auth_context)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Website Import Endpoints ====================

@router.post("/import", response_model=WebsiteImportResponse)
async def import_website(
    request: WebsiteImportRequest,
    auth: ContextAuthentication = Depends(require_ai_access)
) -> WebsiteImportResponse:
    """
    Import a website and convert it to a Nocode page definition.

    This endpoint extracts the visual structure of a website and converts it
    to Nocode components with responsive styles (desktop, tablet, mobile).

    **Features:**
    - Multi-viewport extraction (1440px, 768px, 375px)
    - Automatic image upload to Nocode files service
    - 1:1 mapping of HTML elements to Nocode components
    - CSS property extraction with computed value resolution
    - Responsive styles with proper resolution breakpoints

    **Component Mapping:**
    - `<div>`, `<section>`, `<nav>`, etc. → Grid
    - `<h1>`-`<h6>`, `<p>`, `<span>` → Text
    - `<img>` → Image
    - `<a>` → Link (or Grid with linkPath if has children)
    - `<button>` → Button (or Grid if has complex children)
    - `<input>` → TextBox
    - `<textarea>` → TextArea
    - `<svg>` → Image (as data URI)

    **Example Request:**
    ```json
    {
        "sourceUrl": "https://example.com/landing"
    }
    ```

    **Example Response:**
    ```json
    {
        "success": true,
        "page": {
            "rootComponent": "pageRoot",
            "componentDefinition": { ... }
        },
        "stats": {
            "elementsExtracted": 42,
            "componentsCreated": 85,
            "imagesUploaded": 12
        },
        "errors": []
    }
    ```
    """
    import logging
    logger = logging.getLogger(__name__)

    try:
        # Get client code from auth if not provided
        client_code = request.clientCode
        if auth and auth.clientCode and not client_code:
            client_code = auth.clientCode

        # Build the page agent request
        page_request = PageAgentRequest(
            instruction=f"Import website from {request.sourceUrl}",
            sourceUrl=request.sourceUrl,
            clientCode=client_code,
            options=PageAgentOptions(mode=PageAgentMode.IMPORT)
        )

        # Execute import
        page_agent = get_page_agent()
        result = await page_agent.execute(page_request)

        # Build stats from agent logs
        stats = {
            "elementsExtracted": 0,
            "componentsCreated": len(result.page.get("componentDefinition", {})),
            "imagesUploaded": 0
        }

        # Extract stats from agent logs
        if "extraction" in result.agentLogs:
            log = result.agentLogs["extraction"]
            if log.reasoning:
                import re
                match = re.search(r'(\d+) elements', log.reasoning)
                if match:
                    stats["elementsExtracted"] = int(match.group(1))

        if "image_upload" in result.agentLogs:
            log = result.agentLogs["image_upload"]
            if log.reasoning:
                import re
                match = re.search(r'(\d+) images', log.reasoning)
                if match:
                    stats["imagesUploaded"] = int(match.group(1))

        # Collect errors
        errors = []
        for agent_name, log in result.agentLogs.items():
            if log.status == "error":
                if log.errors:
                    errors.extend(log.errors)
                if log.error:
                    errors.append(log.error)

        return WebsiteImportResponse(
            success=result.success,
            page=result.page,
            stats=stats,
            errors=errors
        )

    except Exception as e:
        logger.error(f"Website import failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/import/stream")
async def import_website_streaming(
    request: WebsiteImportRequest,
    auth: ContextAuthentication = Depends(require_ai_access)
):
    """
    Import a website with SSE streaming progress updates.

    Same as `/import` but returns Server-Sent Events for real-time progress.

    **Event Types:**
    - `status`: Progress messages
    - `phase`: Phase transitions (Visual Extraction, Image Upload, Direct Conversion)
    - `complete`: Final result with page JSON
    - `error`: Error occurred

    **Example Usage (JavaScript):**
    ```javascript
    const eventSource = new EventSource('/api/ai/agent/import/stream', {
        method: 'POST',
        body: JSON.stringify({ sourceUrl: 'https://example.com' })
    });

    eventSource.onmessage = (event) => {
        const data = JSON.parse(event.data);
        console.log(data.message);
    };

    eventSource.addEventListener('complete', (event) => {
        const result = JSON.parse(event.data);
        console.log('Page:', result.data.page);
        eventSource.close();
    });
    ```
    """
    # Get client code from auth if not provided
    client_code = request.clientCode
    if auth and auth.clientCode and not client_code:
        client_code = auth.clientCode

    # Build the page agent request
    page_request = PageAgentRequest(
        instruction=f"Import website from {request.sourceUrl}",
        sourceUrl=request.sourceUrl,
        clientCode=client_code,
        options=PageAgentOptions(mode=PageAgentMode.IMPORT)
    )

    page_agent = get_page_agent()

    return EventSourceResponse(
        stream_page_generation(
            page_request,
            page_agent,
            client_code=client_code,
            auth_context={
                "clientCode": auth.clientCode or (auth.user.clientCode if auth.user else "") if auth else "",
                "clientId": auth.user.clientId if auth and auth.user else 0,
                "userId": auth.user.id if auth and auth.user else 0,
                "appCode": auth.verifiedAppCode or auth.urlAppCode if auth else ""
            } if auth else None
        ),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache, no-store",
            "Connection": "keep-alive",
        }
    )

