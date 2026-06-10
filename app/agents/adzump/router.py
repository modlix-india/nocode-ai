"""Adzump router — chat endpoint.

Common endpoints (models, sessions) are registered via create_common_routes().
Only the /chat endpoint with adzump-specific logic lives here.
"""

from __future__ import annotations

import logging
from typing import Optional, List

from fastapi import APIRouter, Depends
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

logger = logging.getLogger(__name__)

router = APIRouter()
create_common_routes(router, agent_name="adzump")

from app.agents.adzump.agent import AdzumpAgent


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
    return stream_agent_response(
        agent, body.message, session, image_blocks, model_override=body.model
    )


from fastapi import HTTPException


class AddTargetAreaRequest(BaseModel):
    name: str
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    lat: float
    lng: float
    radius: float
    google_id: Optional[str] = None
    meta_key: Optional[str] = None
    place_id: Optional[str] = None


@router.get("/sessions/{session_id}/target-locations/search")
async def search_target_locations(
    session_id: str,
    q: str,
    platform: str,
    auth: AuthContext = Depends(require_auth_context),
):
    session = BaseSession(agent_name="adzump")
    await session.get_or_create(session_id, auth)

    from app.agents.adzump.services.geo.search import search_autocomplete_locations

    try:
        return await search_autocomplete_locations(
            q=q,
            platform=platform,
            client_code=auth.client_code,
            auth_headers=auth.to_headers(),
            session_context=session.context,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/sessions/{session_id}/target-areas")
async def add_target_area(
    session_id: str,
    body: AddTargetAreaRequest,
    auth: AuthContext = Depends(require_auth_context),
):
    session = BaseSession(agent_name="adzump")
    await session.get_or_create(session_id, auth)

    product_data = session.context.setdefault("product_data", {})
    spec = session.context.setdefault("campaign_spec", {})
    platform = (spec.get("platform") or "Google Ads").lower().strip()

    area = {
        "name": body.name,
        "city": body.city,
        "state": body.state,
        "pincode": body.pincode,
        "lat": body.lat,
        "lng": body.lng,
        "distance_km": body.radius,
    }

    if body.place_id:
        area["place_id"] = body.place_id

    if body.google_id:
        area["google_id"] = body.google_id
        area["google_name"] = body.name
    if body.meta_key:
        area["meta_key"] = body.meta_key
        area["meta_name"] = body.name

    target_areas = product_data.setdefault("target_areas", [])
    target_areas.append(area)

    from app.agents.adzump.services.geo.mapping import PlatformGeoMapper

    tool_ctx = {
        "session_context": session.context,
        "auth": auth,
        "client_code": auth.client_code,
        "event_stream": None,
    }

    try:
        mapper = PlatformGeoMapper(session.context, tool_ctx)
        product_data["target_areas"] = await mapper.map_target_areas(
            target_areas, platform
        )
    except Exception as e:
        logger.warning("PlatformGeoMapper mapping failed in add route: %s", e)

    from app.agents.adzump.services.business_storage import save_campaign

    await save_campaign(session.context, tool_ctx)
    await session.save_context()

    from app.agents.adzump.tools.competitor import build_craft2_blocks

    blocks = build_craft2_blocks(product_data, spec)
    return {"blocks": blocks}


@router.delete("/sessions/{session_id}/target-areas/{index}")
async def delete_target_area(
    session_id: str, index: int, auth: AuthContext = Depends(require_auth_context)
):
    session = BaseSession(agent_name="adzump")
    await session.get_or_create(session_id, auth)

    product_data = session.context.setdefault("product_data", {})
    spec = session.context.setdefault("campaign_spec", {})
    platform = (spec.get("platform") or "Google Ads").lower().strip()

    target_areas = product_data.setdefault("target_areas", [])
    if 0 <= index < len(target_areas):
        target_areas.pop(index)
    else:
        raise HTTPException(status_code=400, detail="Invalid target area index")

    from app.agents.adzump.services.geo.mapping import PlatformGeoMapper

    tool_ctx = {
        "session_context": session.context,
        "auth": auth,
        "client_code": auth.client_code,
        "event_stream": None,
    }

    try:
        mapper = PlatformGeoMapper(session.context, tool_ctx)
        product_data["target_areas"] = await mapper.map_target_areas(
            target_areas, platform
        )
    except Exception as e:
        logger.warning("PlatformGeoMapper mapping failed in delete route: %s", e)

    from app.agents.adzump.services.business_storage import save_campaign

    await save_campaign(session.context, tool_ctx)
    await session.save_context()

    from app.agents.adzump.tools.competitor import build_craft2_blocks

    blocks = build_craft2_blocks(product_data, spec)
    return {"blocks": blocks}
