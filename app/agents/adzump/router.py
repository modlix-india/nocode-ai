"""Adzump router — chat endpoint.

Common endpoints (models, sessions) are registered via create_common_routes().
Only the /chat endpoint with adzump-specific logic lives here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.base_auth import require_auth_context
from app.core.base_router import (
    ChatAttachment,
    build_image_blocks,
    create_common_routes,
    stream_agent_response,
)
from app.core.session import BaseSession, AuthContext
from app.core.streaming import AgentEventStream
from app.services.session_manager import get_session_manager
from app.agents.adzump.agent import AdzumpAgent
from app.agents.adzump.agents.geo.agent import get_geo_targeting_agent
from app.agents.adzump.agents.geo.widget import parse_location_widget_message

logger = logging.getLogger(__name__)

router = APIRouter()
create_common_routes(router, agent_name="adzump")


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    model: Optional[str] = None
    attachments: Optional[List[ChatAttachment]] = None


@router.post("/chat")
async def chat(body: ChatRequest, auth: AuthContext = Depends(require_auth_context)):
    """Stream an adzump agent response as SSE."""
    agent = AdzumpAgent.get_instance()

    session = BaseSession(agent_name="adzump")
    await session.get_or_create(body.session_id, auth)

    if not body.session_id:
        title = body.message[:100].strip()
        if title:
            await get_session_manager().update_session_title(
                session.session_id, title, auth.user_id
            )

    image_blocks = build_image_blocks(body.attachments) if body.attachments else None

    # v9 I-0 · stash raw image uploads so manage_assets can persist them
    # as campaign assets. build_image_blocks only formats them for LLM vision
    # (then drops the bytes); the ingest tool needs the raw base64. Overwrites
    # any prior stash — only this turn's uploads are pending ingest.
    if body.attachments:
        session.context["_pending_uploads"] = [
            {"data": a.data, "mime": a.mime_type, "name": a.name}
            for a in body.attachments
            if a.type == "image" and a.data
        ]

    # Location widget messages are machine-readable structured actions from
    # the craft-panel search widget.  They carry all params needed to call
    # modify_targeting_location directly — no LLM reasoning required.
    widget_params = parse_location_widget_message(body.message)
    if widget_params is not None:
        return _stream_location_widget(agent, session, widget_params)

    return stream_agent_response(
        agent, body.message, session, image_blocks, model_override=body.model
    )


def _stream_location_widget(
    agent: AdzumpAgent,
    session: BaseSession,
    params: dict,
) -> StreamingResponse:
    """Execute a location widget action directly (no LLM) and stream SSE."""
    event_stream = AgentEventStream()

    async def run() -> None:
        try:
            # Build the same tool context the agent would pass to any tool.
            ctx = agent.build_tool_context(session)
            ctx["event_stream"] = event_stream
            # Clear any pending chip-question so the next real agent turn
            # doesn't re-ask duration/budget after a location add/delete.
            session.context.pop("_pending_elicitation", None)

            await event_stream.emit_tool_start(
                tool_use_id="widget_location",
                tool_name="modify_targeting_location",
                display_name="Geo Targeting",
                tool_input=params,
            )

            result = await get_geo_targeting_agent().modify(params, ctx)
            # modify() owns save_campaign + _rerender_craft; nothing extra needed here.

            await event_stream.emit_tool_result(
                tool_use_id="widget_location",
                tool_name="modify_targeting_location",
                success=result.success,
                summary=result.summary or result.error or "",
            )
        except Exception as e:
            logger.exception("Location widget action failed")
            await event_stream.emit_error(str(e))
        finally:
            await event_stream.emit_done(session_id=session.session_id)

    async def event_generator():
        task = asyncio.create_task(run())
        try:
            async for event in event_stream.events():
                yield event.to_sse()
        except asyncio.CancelledError:
            task.cancel()
            raise
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/sessions/{session_id}/target-locations/search")
async def search_target_locations(
    session_id: str,
    q: str,
    platform: str = "google",
    auth: AuthContext = Depends(require_auth_context),
):
    session = BaseSession(agent_name="adzump")
    await session.get_or_create(session_id, auth)

    from app.agents.adzump.services.geo.search import search_autocomplete_locations

    loc_meta = session.context.get("_location_meta") or {}
    country_code = loc_meta.get("country_code") or "IN"

    try:
        return await search_autocomplete_locations(
            q=q,
            platform=platform,
            client_code=auth.client_code,
            auth_headers=auth.to_headers(),
            session_context=session.context,
            country_code=country_code,
        )
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Geo location search failed: %s", e)
        raise HTTPException(status_code=500, detail="Geo search unavailable")
