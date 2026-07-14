"""Campaign-creation HTTP endpoints (registered under /api/ai/adzump).

keyword/volume — scores a keyword the user adds in the review panel: resolve the campaign's
ad account + the run's geo from the session, then fetch Google volume via the Planner's
historical-metrics endpoint. Fail-soft: no data → volume 0, so the add always succeeds.

The keyword review-panel widget transport (parse_keyword_widget_message + stream_keyword_widget)
also lives here — the fast path that applies add/edit/delete through update_keywords with no LLM
turn, streamed via the shared SSE wrapper.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.base_auth import require_auth_context
from app.core.base_router import sse_stream_response
from app.core.session import AuthContext, BaseSession
from app.core.streaming import AgentEventStream

from app.agents.adzump.adapters.google import keyword_planner
from app.agents.adzump.agents.campaign.tools.google.keyword_update import (
    update_keywords,
)
from app.agents.adzump.agents.campaign.google.keyword.models import normalize

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_KEYWORDS = 25  # bound a single add request


class KeywordVolumeRequest(BaseModel):
    session_id: str
    keywords: list[str] = Field(default_factory=list)


class KeywordVolume(BaseModel):
    keyword: str
    volume: int = 0
    competition: str = "UNKNOWN"
    cpc_low: float = 0.0
    cpc_high: float = 0.0


class KeywordVolumeResponse(BaseModel):
    results: list[KeywordVolume] = Field(default_factory=list)


@router.post("/keyword/volume", response_model=KeywordVolumeResponse)
async def keyword_volume(
    body: KeywordVolumeRequest,
    auth: AuthContext = Depends(require_auth_context),
) -> KeywordVolumeResponse:
    """Return Google volume/competition/CPC for user-added keywords in the panel."""
    keywords = list(
        dict.fromkeys(
            normalize(k) for k in body.keywords if isinstance(k, str) and k.strip()
        )
    )[:_MAX_KEYWORDS]
    if not keywords:
        return KeywordVolumeResponse()

    session = BaseSession(agent_name="adzump")
    await session.get_or_create(body.session_id, auth)
    sctx = session.context or {}
    spec = sctx.get("campaign_spec") or {}
    customer_id = str(spec.get("account") or "").strip()

    by_kw: dict[str, dict] = {}
    if customer_id:
        # Reuse the same geo the run scored with, so added keywords are comparable.
        geo = ((sctx.get("keyword_research") or {}).get("meta") or {}).get("geo") or {}
        metrics = await keyword_planner.fetch_keyword_historical_metrics(
            keywords,
            **keyword_planner.planner_call_args(
                customer_id=customer_id,
                login_customer_id=str(spec.get("parent_account") or "").strip(),
                client_code=auth.client_code,
                auth_headers=auth.to_headers(),
                geo_target_constants=geo.get("geo_target_constants") or None,
                language=geo.get("language"),
            ),
        )
        by_kw = {m["keyword"]: m for m in metrics}
    else:
        logger.info(
            "keyword_volume: no ad account in session %s — returning 0 volumes",
            body.session_id,
        )

    return KeywordVolumeResponse(
        results=[
            KeywordVolume(
                keyword=k,
                volume=int(by_kw.get(k, {}).get("volume", 0)),
                competition=str(by_kw.get(k, {}).get("competition", "UNKNOWN")),
                cpc_low=float(by_kw.get(k, {}).get("cpc_low", 0.0)),
                cpc_high=float(by_kw.get(k, {}).get("cpc_high", 0.0)),
            )
            for k in keywords
        ]
    )


# Keyword review-panel widget (fast path, no LLM)
#
# The panel posts a structured JSON action (add/edit/delete) as the chat message.
# The router detects it here and streams the mutation directly — no agent turn.


def parse_keyword_widget_message(msg: str) -> dict[str, Any] | None:
    """Return the decoded payload if msg is a keyword-widget JSON action, else None."""
    stripped = msg.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and payload.get("type") == "keyword_widget":
        return payload
    return None


def stream_keyword_widget(
    agent: Any, session: BaseSession, params: dict
) -> StreamingResponse:
    """Fast-path SSE for a keyword panel action: mutate the keyword set and re-emit the
    review block WITHOUT an LLM turn. keepalive off — the mutation finishes immediately."""
    event_stream = AgentEventStream()

    async def run() -> None:
        try:
            ctx = agent.build_tool_context(session)
            ctx["event_stream"] = event_stream
            await event_stream.emit_tool_start(
                tool_use_id="widget_keyword",
                tool_name="update_keyword",
                display_name="Keyword Update",
                tool_input=params,
            )
            result = await update_keywords(params, ctx)
            if result.success:
                await session.save_context()
            await event_stream.emit_tool_result(
                tool_use_id="widget_keyword",
                tool_name="update_keyword",
                success=result.success,
                summary=result.summary or result.error or "",
            )
        except Exception as e:
            logger.exception("Keyword widget action failed")
            await event_stream.emit_error(str(e))
        finally:
            await event_stream.emit_done(session_id=session.session_id)

    return sse_stream_response(event_stream, run(), keepalive=False)
