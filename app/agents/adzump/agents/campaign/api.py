"""Campaign-creation HTTP endpoints (registered under /api/ai/adzump).

keyword/volume — scores a keyword the user adds in the keyword review panel. The
frontend posts the keyword(s) + session_id; we resolve the campaign's ad account and
the geo used for this run from the session, then fetch Google volume via the Planner's
historical-metrics endpoint. Fail-soft: a keyword with no data comes back as volume 0,
so the add always succeeds.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.base_auth import require_auth_context
from app.core.session import AuthContext, BaseSession

from app.agents.adzump.adapters.google import keyword_planner
from app.agents.adzump.agents.keyword.models import normalize

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
    keywords = list(dict.fromkeys(
        normalize(k) for k in body.keywords if isinstance(k, str) and k.strip()
    ))[:_MAX_KEYWORDS]
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
            customer_id=customer_id,
            login_customer_id=str(spec.get("parent_account") or "").strip(),
            client_code=auth.client_code,
            auth_headers=auth.to_headers(),
            geo_target_constants=geo.get("geo_target_constants") or None,
            language=geo.get("language") or keyword_planner.DEFAULT_LANGUAGE,
        )
        by_kw = {m["keyword"]: m for m in metrics}
    else:
        logger.info("keyword_volume: no ad account in session %s — returning 0 volumes", body.session_id)

    return KeywordVolumeResponse(results=[
        KeywordVolume(
            keyword=k,
            volume=int(by_kw.get(k, {}).get("volume", 0)),
            competition=str(by_kw.get(k, {}).get("competition", "UNKNOWN")),
            cpc_low=float(by_kw.get(k, {}).get("cpc_low", 0.0)),
            cpc_high=float(by_kw.get(k, {}).get("cpc_high", 0.0)),
        )
        for k in keywords
    ])
