"""Campaign-creation HTTP endpoints (registered under /api/ai/adzump).

keyword/volume — scores a keyword the user adds in the review panel: resolve the campaign's
ad account + the run's geo from the session, then fetch Google volume via the Planner's
historical-metrics endpoint. Fail-soft: no data → volume 0, so the add always succeeds.

audience/search — segments the panel can offer to add. A segment reference is opaque, so the
panel picks from the same catalogue the build ran on rather than constructing one.

The review-panel widget transport lives here too (parse_widget_message + stream_widget) — the
fast path that applies a panel action with no LLM turn, streamed via the shared SSE wrapper.
Each panel is a row in _WIDGET_MUTATIONS, so the chat router never grows a branch per panel.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agents.adzump.adapters.google import audience_taxonomy, keyword_planner
from app.agents.adzump.agents.campaign.google.audience import catalogue
from app.agents.adzump.agents.campaign.google.keyword.models import normalize
from app.agents.adzump.agents.campaign.models import (
    audience,
    keyword_research,
    resolve_channel,
)
from app.agents.adzump.agents.campaign.tools.google.audience_update import (
    update_audience,
)
from app.agents.adzump.agents.campaign.tools.google.keyword_update import (
    update_keywords,
)
from app.core.base_auth import require_auth_context
from app.core.base_router import sse_stream_response
from app.core.session import AuthContext, BaseSession
from app.core.streaming import AgentEventStream
from app.core.tools.base import ToolResult

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_KEYWORDS = 25  # bound a single add request
_MAX_SEGMENT_RESULTS = 25  # what one panel search offers


class _Panel(NamedTuple):
    """What a panel request needs off the session: the saved state and the account to
    query Google with. ``customer_id`` is "" when none is selected yet - the panel is
    already on screen, so every endpoint here degrades to an empty result, never an error."""

    ctx: dict
    spec: dict
    customer_id: str
    login_customer_id: str


async def _panel(session_id: str, auth: AuthContext) -> _Panel:
    session = BaseSession(agent_name="adzump")
    await session.get_or_create(session_id, auth)
    ctx = session.context or {}
    spec = ctx.get("campaign_spec") or {}
    return _Panel(
        ctx=ctx,
        spec=spec,
        customer_id=str(spec.get("account") or "").strip(),
        login_customer_id=str(spec.get("parent_account") or "").strip(),
    )


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

    panel = await _panel(body.session_id, auth)

    by_kw: dict[str, dict] = {}
    if panel.customer_id:
        # Reuse the same geo the run scored with, so added keywords are comparable.
        geo = ((keyword_research(panel.ctx) or {}).get("meta") or {}).get("geo") or {}
        metrics = await keyword_planner.fetch_keyword_historical_metrics(
            keywords,
            **keyword_planner.planner_call_args(
                customer_id=panel.customer_id,
                login_customer_id=panel.login_customer_id,
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


class AudienceSearchRequest(BaseModel):
    session_id: str
    query: str = ""


class AudienceSegment(BaseModel):
    ref: str
    label: str
    kind: str
    path: list[str] = Field(default_factory=list)


class AudienceSearchResponse(BaseModel):
    results: list[AudienceSegment] = Field(default_factory=list)


@router.post("/audience/search", response_model=AudienceSearchResponse)
async def audience_search(
    body: AudienceSearchRequest,
    auth: AuthContext = Depends(require_auth_context),
) -> AudienceSearchResponse:
    """Segments the panel can offer to add, matching a phrase.

    A segment reference is opaque, so the panel cannot construct one — it has to pick from
    the same catalogue the build ran on. Empty results are a real answer: Google has no
    segment for everything.
    """
    query = body.query.strip()
    if not query:
        return AudienceSearchResponse()

    panel = await _panel(body.session_id, auth)
    if not panel.customer_id:
        logger.info("audience_search: no ad account in session %s", body.session_id)
        return AudienceSearchResponse()

    dump = audience(panel.ctx) or {}
    candidates = await catalogue.load(
        customer_id=panel.customer_id,
        channel_type=resolve_channel(panel.spec).google_channel_type.value,
        country_code=str((dump.get("meta") or {}).get("country") or ""),
        login_customer_id=panel.login_customer_id,
        client_code=auth.client_code,
        auth_headers=auth.to_headers(),
    )
    # Already-targeted segments are dropped rather than shown and refused on click.
    targeted = {s["ref"] for s in dump.get("signals") or []}
    hits = [
        c
        for c in audience_taxonomy.rank_by_name(candidates, query, lambda c: c["label"])
        if c["ref"] not in targeted
    ]
    return AudienceSearchResponse(
        results=[AudienceSegment(**h) for h in hits[:_MAX_SEGMENT_RESULTS]]
    )


# Review-panel widgets (fast path, no LLM)
#
# A panel posts a structured JSON action (add/edit/delete) as the chat message. The router
# sniffs it against this table and streams the mutation directly — no agent turn. A new
# panel adds a row here; nothing else changes, and the router never grows a branch per panel.

_WIDGET_MUTATIONS: dict[str, Callable[[dict, dict], Awaitable[ToolResult]]] = {
    "keyword_widget": update_keywords,
    "audience_widget": update_audience,
}


def parse_widget_message(msg: str) -> tuple[dict[str, Any], Callable] | None:
    """(payload, the mutation that applies it) if msg is a panel action, else None."""
    stripped = msg.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    mutate = _WIDGET_MUTATIONS.get(str(payload.get("type") or ""))
    return (payload, mutate) if mutate is not None else None


def stream_widget(
    agent: Any, session: BaseSession, params: dict, mutate: Callable
) -> StreamingResponse:
    """Fast-path SSE for a panel action: mutate the saved set and re-emit its review block
    WITHOUT an LLM turn. keepalive off — the mutation finishes immediately."""
    event_stream = AgentEventStream()

    async def run() -> None:
        try:
            ctx = agent.build_tool_context(session)
            ctx["event_stream"] = event_stream
            result = await mutate(params, ctx)
            if result.success:
                await session.save_context()
            # The panel already shows the change, so the reply is the confirmation — or the
            # reason it was refused — as prose rather than a tool card.
            await event_stream.emit_text(result.summary or result.error or "")
        except Exception as e:
            logger.exception("Widget action failed: %s", params.get("type"))
            await event_stream.emit_error(str(e))
        finally:
            await event_stream.emit_done(session_id=session.session_id)

    return sse_stream_response(event_stream, run(), keepalive=False)
