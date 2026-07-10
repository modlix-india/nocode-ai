"""Geo-search HTTP route - UI helper for the map widget's typeahead search.

This is a *UI helper*, not part of the orchestrator's chat flow. The map
widget calls this endpoint on every keystroke to populate its search-box
suggestions; the orchestrator's LLM never invokes it. It lives in the
location agent's package (the owner of geo targeting) and is folded into
the adzump chat router (``router.include_router``) so main.py mounts ONE
adzump router: the orchestrator routes intent, this serves UI data.

Why not through the LLM? Typeahead fires per keystroke. An LLM roundtrip
would add ~800ms × 10–20 keystrokes per search = 8–20s of typing lag and
~$0.02 per search in API costs. The Google Places API does this in ~50ms.
The LLM is reserved for interpretation; data fetching uses specialized APIs.

Why a separate HTTP endpoint at all (vs. a frontend-side direct call)?
The frontend cannot call Google Ads' geo-target API directly - it needs
the user's auth headers and the client_code, which only the backend has.
The backend proxies the call.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.agents.adzump.adapters.connections import TokenServiceError
from app.agents.adzump.agents.location.search import search_autocomplete_locations
from app.core.base_auth import require_auth_context, AuthContext
from app.core.session import BaseSession

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Geo Search"])


@router.get("/sessions/{session_id}/target-locations/search")
async def search_target_locations(
    session_id: str,
    q: str,
    platform: str = "google",
    auth: AuthContext = Depends(require_auth_context),
):
    """Typeahead autocomplete for the map widget's search box.

    Returns a list of place candidates matching ``q`` for the active
    platform's geo-target catalog. Read-only; does not mutate session
    state, does not go through the orchestrator LLM, does not appear in
    any conversation history.
    """
    session = BaseSession(agent_name="adzump")
    await session.get_or_create(session_id, auth)

    place = (session.context.get("product_data") or {}).get("place") or {}
    country_code = place.get("country_code") or "IN"

    try:
        return await search_autocomplete_locations(
            q=q,
            platform=platform,
            client_code=auth.client_code,
            auth_headers=auth.to_headers(),
            country_code=country_code,
        )
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except TokenServiceError as e:
        # Lived bug: token failures once rendered as HTTP 200 [] - "search is
        # broken" with a green status. 503 = platform auth outage, retryable.
        logger.error("geo_search_token_outage: platform=%s err=%s", platform, str(e)[:200])
        raise HTTPException(status_code=503, detail="Ad platform authentication unavailable")
    except Exception as e:
        logger.exception("Geo location search failed: %s", e)
        raise HTTPException(status_code=500, detail="Geo search unavailable")