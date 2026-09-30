"""audience_targeting — choose who a Demand Gen campaign reaches.

A campaign-creation step (runs before launch), the Demand Gen counterpart to
``keyword_research``: Search campaigns are built out of keywords, Demand Gen out of audience
segments. Reads the assembled campaign_spec + product_data from session.context, runs the
AudienceAgent once, and shows the chosen segments and demographics in the review panel.

Segment availability is scoped by country, so the country is resolved before the run rather
than left to the agent.
"""

from __future__ import annotations

import asyncio
import logging

from app.agents.adzump.agents.campaign.brief import business_text as _business_text
from app.agents.adzump.agents.campaign.craft import (
    audience_review_block,
    emit_campaign_craft,
    emit_section_update,
)
from app.agents.adzump.agents.campaign.google.audience.agent import get_audience_agent
from app.agents.adzump.agents.campaign.google.audience.constants import (
    BLUEPRINTS_KEY,
    MIN_SIGNALS_TOTAL,
)
from app.agents.adzump.agents.campaign.models import (
    Channel,
    audience,
    resolve_channel,
    set_audience,
)
from app.agents.adzump.agents.location.targeting_run import (
    resolve_country_code,
    resolve_location_name,
)
from app.agents.adzump.platform import to_enum_value as platform_enum_value
from app.core.tools.base import ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_SUPPORTED_PLATFORM = "google"
# Ceiling for one run: load the taxonomy, pick segments, decide demographics, with room to
# search and revise inside the agent's turn budget.
_TARGETING_TIMEOUT_SECONDS = 300
_LOG_TRUNCATE = 200


async def _resolve_country(session_ctx: dict) -> str:
    """Two-letter country code for the availability check, or "" if it cannot be resolved.

    No default: an assumed country would admit segments that cannot serve where the campaign
    runs. Unresolved simply drops the country-scoped entries, which narrows the catalogue
    without producing a wrong campaign.
    """
    product = session_ctx.get("product_data") or {}
    # place can round-trip from persisted JSON as an explicit null; kept on product so the
    # geocode below persists for reuse.
    place = product.get("place")
    if not isinstance(place, dict):
        place = {}
        product["place"] = place

    spec = session_ctx.get("campaign_spec") or {}
    code = await resolve_country_code(resolve_location_name(product, spec), place)
    if not code:
        logger.warning(
            "audience_targeting: country unresolved - catalogue will be thinner"
        )
    return code


# Restricted targeting for housing, employment and credit ads - US and Canada only.
# https://support.google.com/adspolicy/answer/143465
_HEC_COUNTRIES = {"united states": "US", "canada": "CA"}


def _hec_regions(product: dict) -> list[str]:
    """Restricted-targeting countries this campaign reaches, from Google's canonical geo name
    (always ends in the country). Separate from the catalogue country: targeting Mumbai AND
    Austin loads India's catalogue but is still bound by US policy."""
    found: list[str] = []
    for area in product.get("target_areas") or []:
        name = str((area.get("google") or {}).get("name") or "")
        code = _HEC_COUNTRIES.get(name.rsplit(",", 1)[-1].strip().lower())
        if code and code not in found:
            found.append(code)
    return found


def _targeting_key(customer_id: str, product: dict, country: str) -> str:
    """Fingerprint of the inputs a run depends on — used to skip a redundant re-run."""
    return "|".join(
        [
            customer_id,
            str(product.get("product_name", "")),
            str(product.get("business_type", "")),
            country,
        ]
    )


async def _audience_targeting(params: dict, context: dict) -> ToolResult:
    session_ctx = context.get("session_context")
    if session_ctx is None:
        return ToolResult(success=False, error="No session context available.")
    spec = session_ctx.get("campaign_spec") or {}
    product = session_ctx.get("product_data") or {}

    if platform_enum_value(spec.get("platform")) != _SUPPORTED_PLATFORM:
        return ToolResult(
            success=False,
            error="Audience targeting is available for Google campaigns only.",
        )

    channel = resolve_channel(spec)
    if channel is not Channel.DEMAND_GEN:
        return ToolResult(
            success=True,
            summary=f"{channel.value} campaigns target keywords, not audiences — skipped.",
            data={"skipped": True, "channel": channel.value},
        )

    customer_id = str(spec.get("account") or "").strip()
    if not customer_id:
        return ToolResult(
            success=False,
            error="No ad account selected — set the campaign account first.",
        )
    if context.get("auth") is None:
        return ToolResult(
            success=False, error="No auth context for audience targeting."
        )

    country = await _resolve_country(session_ctx)

    stream = context["event_stream"]  # the sub-agent reports its lifecycle through this
    craft_id = (
        session_ctx.get("craft_id") or f"campaign_{context.get('session_id', '')}"
    )
    cached = audience(session_ctx)
    targeting_key = _targeting_key(customer_id, product, country)
    if cached and (cached.get("meta") or {}).get("key") == targeting_key:
        await emit_section_update(
            stream,
            craft_id,
            audience_review_block(cached, session_ctx.get(BLUEPRINTS_KEY)),
        )
        return ToolResult(
            success=True,
            summary="Audience already built for these details — showing the saved set.",
            data={"craft_id": craft_id},
        )

    try:
        result = await asyncio.wait_for(
            get_audience_agent().suggest(
                business_text=_business_text(product),
                ad_account={
                    "customer_id": customer_id,
                    "login_customer_id": str(spec.get("parent_account") or "").strip(),
                },
                channel=channel,
                country_code=country,
                hec_regions=_hec_regions(product),
                parent_event_stream=stream,
                auth=context["auth"],
            ),
            _TARGETING_TIMEOUT_SECONDS,
        )
    # Exception, not BaseException: wait_for raises TimeoutError, which is one, while
    # CancelledError is not — and a cancelled request must stay cancelled rather than be
    # reported back as a failed run the user can retry.
    except Exception as exc:
        logger.warning("audience_targeting failed: %s", str(exc)[:_LOG_TRUNCATE])
        return ToolResult(
            success=False,
            error="Audience targeting failed — check the ad account and retry.",
        )

    if not result.positives:
        # An ad group with no audience cannot run, and grouped mode has no untargeted mode
        # to fall back to.
        return ToolResult(
            success=False,
            error="No audience segments were chosen — check the ad account and retry.",
        )

    result.meta["key"] = targeting_key
    result.meta["craft_id"] = craft_id
    dump = result.model_dump(mode="json")
    # A panel already on screen is updated in place; a full craft re-emit would repaint it.
    panel_exists = bool(cached)
    set_audience(session_ctx, dump)
    if panel_exists:
        await emit_section_update(
            stream,
            craft_id,
            audience_review_block(dump, session_ctx.get(BLUEPRINTS_KEY)),
        )
    else:
        await emit_campaign_craft(stream, craft_id, session_ctx)

    counts = f"{len(result.positives)} segments"
    if not result.demographics.is_empty:
        counts += " with demographic narrowing"
    # A shortfall is reported rather than padded: a thin catalogue is a real answer for a
    # niche business, and inventing segments to reach a floor targets the wrong people.
    note = (
        f" Note: only {len(result.positives)} segments fit this business — a broader"
        " audience may spend better."
        if len(result.positives) < MIN_SIGNALS_TOTAL
        else ""
    )
    return ToolResult(
        success=True,
        summary=f"Audience ready for review — {counts}.{note}",
        data={"craft_id": craft_id},
    )


audience_targeting = ToolDefinition(
    name="audience_targeting",
    description=(
        "Choose the audience for a Google Demand Gen campaign. Reads the campaign account "
        "and business from session.context, picks the audience segments and demographics "
        "that fit, and shows them in the review panel."
    ),
    display_name="Audience Targeting",
    # No parameters: everything the run needs is already in session.context, and the channel
    # is the user's choice at the consent step rather than the model's.
    parameters=[],
    execute=_audience_targeting,
)
