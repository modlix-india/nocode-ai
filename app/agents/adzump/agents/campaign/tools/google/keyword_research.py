"""keyword_research — build one keyword ad group per theme the user chose.

A campaign-creation step (runs before launch). Reads the assembled campaign_spec +
product_data from session.context, derives a BusinessProfile, then runs the
KeywordResearchAgent once per chosen theme in parallel — each returns its own ad group
of positives/negatives (with volumes), shown as a tab in the review panel.

Which ad groups get built is the user's choice (``campaign_spec["ad_groups"]``, from the
consent step), never the model's; each one runs the keyword theme of the same id — see
``resolve_theme_ids``.

Google Search only for now; other platforms and campaign types become sibling tools.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.agents.adzump._shared import build_ds_headers
from app.agents.adzump.adapters.google import keyword_planner
from app.agents.adzump.agents.campaign.brief import business_text as _business_text
from app.agents.adzump.agents.campaign.craft import (
    emit_campaign_craft,
    emit_section_update,
    keyword_review_block,
)
from app.agents.adzump.agents.campaign.google.keyword.agent import (
    get_keyword_research_agent,
)
from app.agents.adzump.agents.campaign.google.keyword.brief import (
    resolve_location as _resolve_location,
)
from app.agents.adzump.agents.campaign.google.keyword.models import (
    AdGroupStatus,
    BusinessProfile,
    KeywordResearchResult,
    KeywordSet,
)
from app.agents.adzump.agents.campaign.google.keyword.taxonomy import (
    derive_offering_taxonomy,
)
from app.agents.adzump.agents.campaign.google.keyword.themes import (
    resolve_theme_ids,
)
from app.agents.adzump.agents.campaign.models import (
    Channel,
    resolve_channel,
    set_keyword_research,
)
from app.agents.adzump.agents.campaign.models import (
    keyword_research as _saved_keywords,
)
from app.agents.adzump.agents.location.targeting_run import (
    resolve_coordinates,
    resolve_country_geo_constant,
    resolve_location_name,
)
from app.agents.adzump.platform import to_enum_value as platform_enum_value
from app.agents.adzump.services.business_storage import resolve_url
from app.core.tools.base import ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_SUPPORTED_PLATFORM = "google"
# Ceiling for one ad group's full run. A hit leaves whatever phases finished (-> partial).
_RESEARCH_TIMEOUT_SECONDS = 500
_LOG_TRUNCATE = 200


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


def _research_key(customer_id: str, product: dict, geo_constant: str) -> str:
    """Fingerprint of the inputs that decide whether an ALREADY-RESEARCHED ad group is still
    valid. The chosen ad groups are deliberately not part of it: a theme's run reads none of
    the others, so asking for one more would otherwise invalidate the key and re-research -
    discarding - the sets the user has already reviewed.
    """
    return "|".join(
        [
            customer_id,
            str(product.get("product_name", "")),
            str(product.get("business_type", "")),
            geo_constant,
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

    # Channel gate — Search uses keywords; audience-targeted channels don't. Through
    # resolve_channel, the same reading audience_targeting gates on: two gates parsing the
    # spec differently is how a campaign ends up with neither tool running.
    channel = resolve_channel(spec)
    if channel is not Channel.SEARCH:
        return ToolResult(
            success=True,
            summary=f"{channel.value} campaigns don't use keywords — skipped.",
            data={"skipped": True, "channel": channel.value},
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
    themes = resolve_theme_ids(spec)

    login_customer_id = str(spec.get("parent_account") or "").strip()

    # Resolve country-level geo target before the idempotency key so a location
    # change triggers a fresh research run.
    geo = await _resolve_geo(session_ctx, context)

    # Idempotency is per ad group: on the same inputs, one that already has keywords is
    # carried forward and only the rest are researched, so a retry costs the ad group that
    # needs it rather than the whole run.
    geo_constants = geo.get("geo_target_constants") or [""]
    research_key = _research_key(customer_id, product, geo_constants[0])
    cached = _saved_keywords(session_ctx)
    craft_id = (
        session_ctx.get("craft_id") or f"campaign_{context.get('session_id', '')}"
    )
    same_inputs = (
        cached is not None and (cached.get("meta") or {}).get("key") == research_key
    )
    cached_themes = ((cached or {}).get("themes") or {}) if same_inputs else {}
    carried = {
        t: cached_themes[t]
        for t in themes
        if (cached_themes.get(t) or {}).get("positives")
    }
    to_run = [t for t in themes if t not in carried]
    # A panel already on screen is updated in place; a full craft re-emit would repaint it.
    panel_exists = bool(cached)

    if not to_run:
        await emit_section_update(
            context.get("event_stream"), craft_id, keyword_review_block(cached or {})
        )
        return ToolResult(
            success=True,
            summary="Keywords already researched for these details — showing the saved set.",
            data={"craft_id": craft_id},
        )

    # Offering taxonomy (core vs sibling + local-vs-national) — product analysis doesn't
    # persist one, so derive it from confirmed product_data. The key guards a second call
    # within this run; across runs it rides back in meta["taxonomy"] for the manage path.
    tax_key = _taxonomy_key(product)
    cache = session_ctx.get("_offering_taxonomy") or {}
    if cache.get("key") == tax_key:
        taxonomy = cache["data"]
    else:
        # The only step before the workers that is neither logged nor timed anywhere else,
        # so a slow run cannot otherwise be told apart from a slow start.
        started = time.monotonic()
        derived = await derive_offering_taxonomy(product)
        logger.info("kw_taxonomy derived in %ds", int(time.monotonic() - started))
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
    common = {
        "ad_account": {
            "customer_id": customer_id,
            "login_customer_id": login_customer_id,
        },
        "geo": geo,
        "parent_event_stream": context.get("event_stream"),
        "auth": context["auth"],
        "category": taxonomy.get("primary_offering") or profile.category,
        "core_terms": taxonomy.get("core_terms") or [],
        "siblings": taxonomy.get("sibling_categories") or [],
        "location": loc_text,
        "service_areas": service_areas,
        "business_url": resolve_url(session_ctx) or "",
        "business_text": _business_text(product),
    }

    # Themes are independent: one failing or timing out still returns the others, and a
    # finished ad group reaches the panel without waiting for the slowest.
    partials: dict[str, KeywordSet] = {}

    async def _run(
        theme_id: str,
    ) -> tuple[str, KeywordSet | None, BaseException | None]:
        try:
            res = await asyncio.wait_for(
                agent.research(
                    keyword_type=theme_id,
                    sources=profile.source_names(theme_id),
                    partial_sink=partials,
                    **common,
                ),
                _RESEARCH_TIMEOUT_SECONDS,
            )
            return theme_id, res, None
        # BaseException, not Exception: CancelledError doesn't subclass Exception (3.8+).
        except BaseException as exc:
            # A timed-out run leaves whatever phases finished in the sink.
            return theme_id, partials.get(theme_id), exc

    # meta: geo for the add-keyword endpoint, key for the idempotency guard.
    bundle = KeywordResearchResult(
        meta={
            "craft_id": craft_id,
            "channel": channel.value,
            "geo": geo,
            "key": research_key,
            # The panel's edit gate has no LLM, so it reads the brand the taxonomy named.
            "brand_terms": taxonomy.get("brand_terms") or [],
            # The whole taxonomy rides back with the build: it is derived in this throwaway
            # sub-session, so the manage path would otherwise seed every later run with no
            # core terms and no siblings.
            "taxonomy": taxonomy,
        }
    )
    counts: list[str] = []
    failed: list[str] = []
    # Ad groups from an earlier run keep the keywords they already have.
    for theme_id, kset in carried.items():
        bundle.themes[theme_id] = KeywordSet(**kset)
        counts.append(
            f"{theme_id} {len(kset.get('positives') or [])}+/"
            f"{len(kset.get('negatives') or [])}- (kept)"
        )
    pending = set(to_run)
    stream = context.get("event_stream")
    emitted = panel_exists

    for finished in asyncio.as_completed([_run(t) for t in to_run]):
        theme_id, res, exc = await finished
        pending.discard(theme_id)
        if exc is not None:
            # A timeout and a cancel both stringify to "", so the type carries the reason.
            logger.warning(
                "keyword_research %s failed after %ds: %s%s",
                theme_id,
                _RESEARCH_TIMEOUT_SECONDS,
                type(exc).__name__,
                f": {str(exc)[:_LOG_TRUNCATE]}" if str(exc) else "",
            )
        if res is None or not res.positives:
            # An ad group with no keywords can't run: a failed tab, not an empty one.
            failed.append(theme_id)
        else:
            bundle.themes[theme_id] = res
            counts.append(
                f"{theme_id} {len(res.positives)}+/{len(res.negatives)}-"
                + (" (partial)" if res.status == AdGroupStatus.PARTIAL else "")
            )
        # Nothing to show until at least one ad group has keywords. An empty bundle is
        # still truthy, so persisting it would tell the user their campaign is "ready".
        if not bundle.themes:
            continue
        bundle.meta[AdGroupStatus.PENDING.value] = sorted(pending)
        bundle.meta[AdGroupStatus.FAILED.value] = failed
        dump = bundle.model_dump(mode="json")
        set_keyword_research(session_ctx, dump)
        if emitted:
            await emit_section_update(stream, craft_id, keyword_review_block(dump))
        else:
            await emit_campaign_craft(stream, craft_id, session_ctx)
            emitted = True

    if not bundle.themes:
        return ToolResult(
            success=False,
            error="Keyword research produced no results — check the ad account and retry.",
        )

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
