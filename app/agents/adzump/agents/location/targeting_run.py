"""Step helpers behind ``LocationAgent.handle()`` - one targeting run.

Leaf-helper module: the agent method orchestrates; each step below is pure
and never imports the agent. In call order:

    1. validate_run_context   → bail with ToolResult(error) if no auth
    2. resolve_location_name  → product_data.place / spec
    3. resolve_coordinates    → product_data.place OR geocode via google_maps_client
       resolve_country_geo_constant → place.country_geo_constant (best-effort)
    4. build_sub_session      → BaseSession with shared context refs
       (the agent then runs its LLM loop on that sub-session)
    5. build_run_prompt       → business profile + current targeting list +
                                the user's verbatim request
    6. build_run_result       → ToolResult(audience="both", summary, ...)

The success signal is the magic key ``GEO_FINALIZED_KEY`` stamped by
``tools._shared.finalize_targets`` - the funnel EVERY mutation ends in
(discovery tools and add/delete alike). A chatty run that never called a
tool must not read as success - that's enforced by ``build_run_result``.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.adzump._shared import product_location_str
from app.agents.adzump.adapters.google.maps import google_maps_client
from app.agents.adzump.agents.location.tools._shared import GEO_FINALIZED_KEY
from app.core.session import BaseSession
from app.core.tools.base import ToolResult

logger = logging.getLogger(__name__)


# ── Step 1: context validation ──────────────────────────────────────────────
def validate_run_context(context: dict) -> ToolResult | None:
    """Return an error ToolResult if auth or session context is missing, else None.

    session_context is what the whole run mutates - without it the tools would
    write into an orphan dict and report a success the parent never sees."""
    if context.get("auth") is None:
        return ToolResult(
            success=False,
            error="No auth context available for the location agent.",
        )
    if context.get("session_context") is None:
        return ToolResult(
            success=False,
            error="No session context available for the location agent.",
        )
    return None


# ── Step 2: location name resolution ────────────────────────────────────────
def resolve_location_name(product: dict, spec: dict) -> str | None:
    """Pick the business location name: confirmed place address, else spec."""
    return product_location_str(product) or spec.get("location")


# ── Step 3: coordinate resolution ───────────────────────────────────────────
async def resolve_coordinates(
    location_name: str | None, place: dict
) -> dict[str, Any] | None:
    """Reuse cached coords from ``place`` (product_data.place); geocode if absent.
    A successful geocode stamps coords + country_code + address back onto it.
    Returns the coords (plus ``country`` name on a fresh geocode) or None."""
    if place.get("lat") is not None and place.get("lng") is not None:
        return {"lat": float(place["lat"]), "lng": float(place["lng"])}

    if not location_name:
        return None

    try:
        geo = await google_maps_client.geocode(location_name)
    except Exception as e:
        logger.warning("Geocoding '%s' failed: %s", location_name, e)
        return None

    if not geo or geo.get("lat") is None or geo.get("lng") is None:
        return None

    place["lat"] = geo["lat"]
    place["lng"] = geo["lng"]
    if geo.get("country_code"):
        place["country_code"] = geo["country_code"]
    if not place.get("address") and geo.get("address"):
        place["address"] = geo["address"]
    return {
        "lat": float(geo["lat"]),
        "lng": float(geo["lng"]),
        "country": geo.get("country"),
    }


async def resolve_country_code(location_name: str | None, place: dict) -> str:
    """The two-letter country for ``place``, geocoding by name if it isn't recorded yet.

    Separate from ``resolve_coordinates``, which returns early once lat/lng are cached and
    only stamps ``country_code`` on a *fresh* geocode - so a place confirmed by map pin
    carries coordinates and no country. Stamps what it finds, best-effort."""
    if code := str(place.get("country_code") or "").strip():
        return code.upper()
    if not location_name:
        return ""
    try:
        geo = await google_maps_client.geocode(location_name)
    except Exception as e:
        logger.warning("Country lookup for '%s' failed: %s", location_name, e)
        return ""
    code = str((geo or {}).get("country_code") or "").strip()
    if code:
        place["country_code"] = code
    return code.upper()


# ── Step 3b: country geo constant ───────────────────────────────────────────
async def resolve_country_geo_constant(
    place: dict, country_name: str, client_code: str, auth_headers: dict
) -> None:
    """Stamp place.country_geo_constant (Google geoTargetConstants/{id} for the
    country) once per session. Best-effort: failure never blocks the run."""
    if place.get("country_geo_constant") or not (
        country_name and place.get("country_code")
    ):
        return
    from app.agents.adzump.adapters.google.client import google_ads_client

    try:
        results = await google_ads_client.suggest_geo_targets(
            query=country_name,
            country_code=place["country_code"],
            client_code=client_code,
            auth_headers=auth_headers,
        )
    except Exception as e:
        logger.warning("Country geo-constant lookup failed for %r: %s", country_name, e)
        return
    for suggestion in results.get("geoTargetConstantSuggestions") or []:
        constant = suggestion.get("geoTargetConstant") or {}
        if constant.get("targetType") == "Country" and constant.get("resourceName"):
            place["country_geo_constant"] = constant["resourceName"]
            return


# ── Step 4: sub-session construction ───────────────────────────────────────
async def build_sub_session(
    parent_ctx: dict, auth, chat_session_id: str = ""
) -> BaseSession:
    """Create a sub-session with shared context refs but isolated message history.

    Shared dict refs let the sub-agent's tools write through to the parent
    (same objects in memory). The keys below cover everything
    ``finalize_targets`` / ``save_campaign`` read. The sub-agent's MESSAGE
    HISTORY stays isolated - that's the real isolation win.
    """
    sub_session = BaseSession(agent_name="location_agent")
    await sub_session.get_or_create(None, auth)

    shared: dict[str, Any] = {
        "product_data": parent_ctx.setdefault("product_data", {}),
        "product_profile": parent_ctx.setdefault("product_profile", {}),
        "campaign_spec": parent_ctx.setdefault("campaign_spec", {}),
        "account_names": parent_ctx.setdefault("account_names", {}),
        "craft_id": parent_ctx.get("craft_id", ""),
        "_craft_id": parent_ctx.get("_craft_id", ""),
        # Parent chat session id - save_campaign stamps it on the record so
        # storage provenance points at the conversation, not this sub-session.
        "_session_id": chat_session_id,
    }
    if isinstance(parent_ctx.get("competitor_analysis"), dict):
        shared["competitor_analysis"] = parent_ctx["competitor_analysis"]

    sub_session.context = shared
    return sub_session


# ── Step 5: prompt construction ────────────────────────────────────────────
def build_run_prompt(
    product: dict, location_name: str, country_code: str, user_request: str
) -> str:
    """User prompt for one run - business profile, current list, verbatim request."""
    summary = (product.get("summary") or "").strip()
    if len(summary) > 600:
        summary = summary[:600] + "…"
    return (
        "Business profile:\n"
        f"- Business: {product.get('product_name') or '(unknown)'}\n"
        f"- Category: {product.get('business_type') or '(unknown)'}\n"
        f"- Operating scale: {(product.get('business_scale') or 'national').strip().lower()}\n"
        f"- Target country: {country_code}\n"
        f"- Confirmed location: {location_name or '(none)'}\n"
        f"- Summary: {summary or '(none)'}\n\n"
        'Current targeting list (1-based - "the second area" is index 2):\n'
        f"{format_current_areas(product.get('target_areas'))}\n\n"
        f'User request:\n"""{user_request}"""\n\n'
        "Do exactly what the request asks with ONE tool call, then write "
        "your 1-2 sentence summary."
    )


def format_current_areas(areas: list[dict] | None) -> str:
    """Render the current targeting list as a numbered index → name map."""
    if not areas:
        return "  (empty - no areas targeted yet)"
    lines = []
    for i, area in enumerate(areas, start=1):
        name = (area or {}).get("name") or "(unnamed)"
        lines.append(f"  {i}. {name}")
    return "\n".join(lines)


# ── Step 6: result construction ─────────────────────────────────────────────
def build_run_result(sub_session: BaseSession, product: dict, spec: dict) -> ToolResult:
    """Compose the ToolResult from the post-run state.

    Returns:
        success=False if ``GEO_FINALIZED_KEY`` was never set (no mutation
        reached finalize - a chatty no-op run, or a failed edit like an
        out-of-range delete; the agent's own final text carries the reason).
        success=True with audience="both" otherwise.
    """
    final_text = _extract_final_summary(sub_session)

    if not sub_session.context.get(GEO_FINALIZED_KEY):
        return ToolResult(
            success=False,
            error=(
                final_text.strip()
                or "The location agent finished without changing the targeting. "
                "Retry manage_targeting_locations with the user's message, or "
                "ask the user to clarify what they want targeted."
            ),
        )

    mapped = product.get("target_areas") or []
    platform = (spec.get("platform") or "Google Ads").strip()
    logger.info(
        "location_agent.run: %d areas for platform=%s",
        len(mapped),
        platform,
    )

    user_summary = (
        final_text.strip()
        or f"Updated targeting - {len(mapped)} area(s) mapped for {platform}."
    )
    model_summary = (
        f"Targeting updated: {len(mapped)} areas for {platform}. "
        f"Summary: {user_summary}"
    )
    # audience="both" - orchestrator reasons over the summary later (State
    # block, _next_action), AND the framework emits it as chat text so the
    # user sees it without depending on the orchestrator LLM's lead-in (the
    # historic dead-end path documented at AGENT.md). Matches
    # `analyze_competitors`'s audience="both" pattern.
    return ToolResult(
        success=True,
        data={"target_areas": mapped, "summary": final_text},
        summary=user_summary,
        audience="both",
        model_summary=model_summary,
    )


def _extract_final_summary(sub_session: BaseSession) -> str:
    """Walk the sub-session messages and return the last assistant text."""
    for msg in reversed(sub_session.get_messages()):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [c.get("text", "") for c in content if c.get("type") == "text"]
            if any(parts):
                return "\n".join(p for p in parts if p)
    return ""
