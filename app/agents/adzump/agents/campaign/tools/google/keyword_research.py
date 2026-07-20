"""keyword_research — build one keyword ad group per theme the user chose.

A campaign-creation step (runs before launch). Reads the assembled campaign_spec +
product_data from session.context, derives a BusinessProfile, then runs the
KeywordResearchAgent once per chosen theme in parallel — each returns its own ad group
of positives/negatives (with volumes), shown as a tab in the review panel.

Which ad groups get built is the user's choice (``campaign_spec["ad_groups"]``, from the
consent step), never the model's; each one runs the keyword theme of the same id — see
``_resolve_themes``.

Google Search only for now; other platforms and campaign types become sibling tools.
"""

from __future__ import annotations

import asyncio
import logging

from app.core.tools.base import ToolDefinition, ToolResult

from app.agents.adzump._shared import build_ds_headers
from app.agents.adzump.adapters.google import keyword_planner
from app.agents.adzump.platform import to_enum_value as platform_enum_value
from app.agents.adzump.services.business_storage import resolve_url

from app.agents.adzump.agents.campaign.craft import emit_campaign_craft
from app.agents.adzump.agents.campaign.google.keyword.agent import get_keyword_research_agent
from app.agents.adzump.agents.campaign.google.keyword.brief import (
    business_text as _business_text,
    resolve_location as _resolve_location,
)
from app.agents.adzump.agents.campaign.google.keyword.themes import (
    DEFAULT_THEME_IDS,
    KEYWORD_THEMES,
)
from app.agents.adzump.agents.campaign.google.keyword.models import (
    BusinessProfile,
    KeywordResearchResult,
)
from app.agents.adzump.agents.campaign.google.keyword.taxonomy import derive_offering_taxonomy
from app.agents.adzump.agents.location.targeting_run import (
    resolve_coordinates,
    resolve_country_geo_constant,
    resolve_location_name,
)

logger = logging.getLogger(__name__)

_SUPPORTED_PLATFORM = "google"
_RESEARCH_TIMEOUT_SECONDS = 300
_LOG_TRUNCATE = 200


def _resolve_themes(spec: dict) -> list[str]:
    """The themes to run, from the ad groups the user chose (each runs the theme of its id).

    Normalises whatever the consent step lands — chip, typed, or nothing. Unknown names are
    dropped rather than guessed; no choice means the full plan we showed them.
    """
    raw = spec.get("ad_groups")
    if isinstance(raw, str):
        raw = raw.replace("&", ",").split(",")
    elif not isinstance(raw, (list, tuple, set)):
        raw = []

    chosen: list[str] = []
    for item in raw:
        # Accept an id ("brand") or a chip label ("Brand only", "Both Brand & Generic").
        for word in str(item).lower().replace("-", " ").split():
            if word in KEYWORD_THEMES and word not in chosen:
                chosen.append(word)
    return chosen or list(DEFAULT_THEME_IDS)


async def _resolve_geo(session_ctx: dict, context: dict) -> dict:
    """Country-level geo for keyword research, from the confirmed Place the location
    agent populates (city volumes read too low to show a user)."""
    product = session_ctx.get("product_data") or {}
    # place can round-trip from persisted JSON as an explicit null, so a setdefault would
    # hand back None (key present) and the .get below would crash — normalise to a dict,
    # kept on product so the resolve chain's writes below persist for reuse.
    place = product.get("place")
    if not isinstance(place, dict):
        place = {}
        product["place"] = place
    geo_const = (place.get("country_geo_constant") or "").strip()

    # Location agent resolves this best-effort; if it didn't run, reuse its own chain.
    if not geo_const:
        spec = session_ctx.get("campaign_spec") or {}
        coords = await resolve_coordinates(resolve_location_name(product, spec), place)
        await resolve_country_geo_constant(
            place,
            (coords or {}).get("country") or "",
            context.get("client_code", ""),
            build_ds_headers(context),
        )
        geo_const = (place.get("country_geo_constant") or "").strip()

    # [] (never [""]) so an unresolved geo falls to the Planner's country default.
    return {
        "geo_target_constants": [geo_const] if geo_const else [],
        "hl": "en",
        "gl": place.get("country_code") or "IN",
        "language": keyword_planner.DEFAULT_LANGUAGE,
    }


def _research_key(
    customer_id: str, product: dict, themes: list[str], geo_constant: str
) -> str:
    """Fingerprint of the inputs a run depends on — used to skip a redundant re-run."""
    return "|".join(
        [
            customer_id,
            str(product.get("product_name", "")),
            str(product.get("business_type", "")),
            geo_constant,
            ",".join(sorted(themes)),
        ]
    )


def _taxonomy_key(product: dict) -> str:
    """Offering fingerprint — re-derive the taxonomy only when the product changes."""
    return f"{product.get('product_name', '')}|{product.get('business_type', '')}"


def _business_profile(product: dict, taxonomy: dict) -> BusinessProfile:
    # category hint = business_type (the taxonomy refines it). The taxonomy also decides
    # which autosuggest surfaces fit this business — YouTube for an informational funnel —
    # data-driven per run, no hardcoded verticals.
    return BusinessProfile(
        category=(
            product.get("business_type") or product.get("product_name") or ""
        ).strip(),
        includes_informational_funnel=bool(
            taxonomy.get("includes_informational_funnel", False)
        ),
    )


async def _keyword_research(params: dict, context: dict) -> ToolResult:
    session_ctx = context.get("session_context")
    if session_ctx is None:
        return ToolResult(success=False, error="No session context available.")
    spec = session_ctx.get("campaign_spec") or {}
    product = session_ctx.get("product_data") or {}

    if platform_enum_value(spec.get("platform")) != _SUPPORTED_PLATFORM:
        return ToolResult(
            success=False,
            error="Keyword research is available for Google Search campaigns only.",
        )

    # Channel gate — Search uses keywords; PMax/others don't (future campaign types).
    channel = str(params.get("channel") or spec.get("channel") or "SEARCH").upper()
    if channel != "SEARCH":
        return ToolResult(
            success=True,
            summary=f"{channel} campaigns don't use keywords — skipped.",
            data={"skipped": True, "channel": channel},
        )

    customer_id = str(spec.get("account") or "").strip()
    if not customer_id:
        return ToolResult(
            success=False,
            error="No ad account selected — set the campaign account first.",
        )
    if context.get("auth") is None:
        return ToolResult(success=False, error="No auth context for keyword research.")

    # The user chose these at the consent step; the model does not get to pick.
    themes = _resolve_themes(spec)

    login_customer_id = str(spec.get("parent_account") or "").strip()

    # Resolve country-level geo target before the idempotency key so a location
    # change triggers a fresh research run.
    geo = await _resolve_geo(session_ctx, context)

    # Idempotency: same inputs already researched -> re-show, don't re-run.
    geo_constants = geo.get("geo_target_constants") or [""]
    research_key = _research_key(customer_id, product, themes, geo_constants[0])
    cached = session_ctx.get("keyword_research")
    craft_id = (
        session_ctx.get("craft_id") or f"campaign_{context.get('session_id', '')}"
    )
    if cached and (cached.get("meta") or {}).get("key") == research_key:
        await emit_campaign_craft(context.get("event_stream"), craft_id, session_ctx)
        return ToolResult(
            success=True,
            summary="Keywords already researched for these details — showing the saved set.",
            data={"craft_id": craft_id},
        )

    # Offering taxonomy (core vs sibling + local-vs-national) — product analysis doesn't
    # persist one, so derive it from confirmed product_data; cached by offering fingerprint
    # (re-derives only when the product changes).
    tax_key = _taxonomy_key(product)
    cache = session_ctx.get("_offering_taxonomy") or {}
    if cache.get("key") == tax_key:
        taxonomy = cache["data"]
    else:
        derived = await derive_offering_taxonomy(product)
        taxonomy = derived.model_dump()
        # `complete` is False only for a transient fail-soft fallback — don't cache that
        # (it would poison the session for this product); a real derivation is cached so
        # it re-derives only when the offering changes.
        cacheable = taxonomy.pop("complete", True)
        if cacheable:
            session_ctx["_offering_taxonomy"] = {"key": tax_key, "data": taxonomy}

    loc_text, service_areas = _resolve_location(
        product, taxonomy.get("is_location_specific", True)
    )
    profile = _business_profile(product, taxonomy)
    agent = get_keyword_research_agent()
    common = dict(
        ad_account={
            "customer_id": customer_id,
            "login_customer_id": login_customer_id,
        },
        geo=geo,
        parent_event_stream=context.get("event_stream"),
        auth=context["auth"],
        category=taxonomy.get("primary_offering") or profile.category,
        core_terms=taxonomy.get("core_terms") or [],
        siblings=taxonomy.get("sibling_categories") or [],
        location=loc_text,
        service_areas=service_areas,
        business_url=resolve_url(session_ctx) or "",
        business_text=_business_text(product),
    )

    # Brand & generic independent — one failing/timing-out still returns the other.
    # Sources are per keyword_type: YouTube joins only the generic run (see source_names).
    results = await asyncio.gather(
        *(
            asyncio.wait_for(
                agent.research(
                    keyword_type=t, sources=profile.source_names(t), **common
                ),
                _RESEARCH_TIMEOUT_SECONDS,
            )
            for t in themes
        ),
        return_exceptions=True,
    )

    # meta: geo for the add-keyword endpoint, key for the idempotency guard.
    bundle = KeywordResearchResult(
        meta={
            "craft_id": craft_id,
            "channel": channel,
            "geo": geo,
            "key": research_key,
        }
    )
    counts: list[str] = []
    failed: list[str] = []
    for t, res in zip(themes, results):
        # BaseException, not Exception: CancelledError doesn't subclass Exception (3.8+).
        if isinstance(res, BaseException):
            logger.warning(
                "keyword_research %s failed: %s", t, str(res)[:_LOG_TRUNCATE]
            )
            failed.append(t)
            continue
        bundle.themes[t] = res
        counts.append(f"{t} {len(res.positives)}+/{len(res.negatives)}-")
    if failed:
        bundle.meta["failed"] = failed

    # Persist ONLY if something was actually built. An empty bundle is still truthy, so
    # storing it would flip keyword_research_done and tell the user their campaign is
    # "ready in the panel" when no craft was ever emitted.
    if not bundle.themes:
        return ToolResult(
            success=False,
            error="Keyword research produced no results — check the ad account and retry.",
        )

    dump = bundle.model_dump(mode="json")
    session_ctx["keyword_research"] = dump
    await emit_campaign_craft(context.get("event_stream"), craft_id, session_ctx)
    note = (
        f" Note: {', '.join(failed)} could not be researched this time."
        if failed
        else ""
    )
    return ToolResult(
        success=True,
        summary="Keywords ready for review — "
        + ", ".join(counts)
        + " (positives/negatives)."
        + note,
        data={"craft_id": craft_id},
    )


keyword_research = ToolDefinition(
    name="keyword_research",
    description=(
        "Research keywords for a Google Search campaign — one ad group per theme the "
        "user chose. Reads the campaign account, business, and chosen themes from "
        "session.context, runs them in parallel, and shows positives + negatives (with "
        "volumes) in the review panel."
    ),
    display_name="Keyword Research",
    # No parameters: which ad groups to build is the USER's choice (campaign_spec.themes,
    # set at the consent step), never the model's.
    parameters=[],
    execute=_keyword_research,
)

GOOGLE_CAMPAIGN_TOOLS = [keyword_research]
