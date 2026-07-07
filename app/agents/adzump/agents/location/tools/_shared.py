"""Shared post-processing for location-agent actions.

Both LLM-callable tools (discover_neighborhoods, geocode_recommendations) and
the agent's deterministic ``add_location``/``delete_location`` methods end the
same way: map the areas to the active platform, write session state, persist,
re-render the craft panel, and re-emit the location chips.
``finalize_targets`` is that single ending - one source of truth for
"targets changed".

Everything here is deterministic Python. No LLM.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.adzump._shared import primary_screenshot_url
from app.agents.adzump.agents.location.platform_mapping import PlatformGeoMapper
from app.agents.adzump.services.business_storage import save_campaign, resolve_url
from app.agents.adzump.tools.craft import emit_craft_panel

logger = logging.getLogger(__name__)

# Success marker for a targeting write. Stamped below; read by
# targeting_run.build_run_result to tell "a tool landed targets" apart
# from "the model chatted and stopped".
GEO_FINALIZED_KEY = "_geo_finalized"


async def finalize_targets(
    resolved: list[dict[str, Any]],
    context: dict,
) -> list[dict[str, Any]]:
    """Map resolved areas to the active platform and make the result durable.

    map → write session state → emit location chips → save_campaign →
    re-render craft. Sets ``session_context["_geo_finalized"]`` so
    ``targeting_run.build_run_result`` can tell "a tool actually landed
    targets" apart from "the model chatted and stopped". Returns the mapped list.
    """
    session_ctx = context.get("session_context") or {}
    product = session_ctx.setdefault("product_data", {})
    spec = session_ctx.get("campaign_spec") or {}
    platform = (spec.get("platform") or "Google Ads").strip()
    loc_meta = session_ctx.setdefault("_location_meta", {})
    country_code = loc_meta.get("country_code") or "IN"

    mapped = resolved
    try:
        mapper = PlatformGeoMapper(context)
        mapped = await mapper.map_target_areas(resolved, platform, country_code)
    except Exception as e:
        # Keep the unmapped list rather than dropping the targets - the next
        # re-map (add/delete/re-discover) resolves handles for what's missing.
        logger.warning("PlatformGeoMapper failed in finalize_targets: %s", e)

    # Single write - the platform handle rides nested on each area, so no
    # per-platform copies. "Mapped for X" checks = platform.is_mapped_for.
    product["target_areas"] = mapped

    await save_campaign(session_ctx, context)
    await rerender_craft(session_ctx, context, product, platform)

    # Chips AFTER save + craft re-render - same SSE order the pre-refactor
    # service used, so the widget never renders chips against a stale panel.
    stream = context.get("event_stream")
    if stream and mapped:
        try:
            await stream.emit_data(
                "suggested_locations",
                {
                    "locations": [a["name"] for a in mapped if a.get("name")],
                    "targeting_type": product.get("business_scale", "national"),
                    "location": (
                        loc_meta.get("address") or spec.get("location") or ""
                    ),
                },
            )
        except Exception as e:
            logger.debug("suggested_locations emit failed: %s", e)

    session_ctx[GEO_FINALIZED_KEY] = True
    return mapped


async def rerender_craft(
    session_ctx: dict, context: dict, product: dict, platform: str
) -> None:
    """Re-emit the craft panel after targeting changes.

    Does NOT persist - callers must save before calling this."""
    stream = context.get("event_stream")
    craft_id = session_ctx.get("craft_id") or session_ctx.get("_craft_id")
    url = resolve_url(session_ctx)
    if stream and craft_id and url:
        try:
            competitive = session_ctx.get("competitor_analysis") or {"competitors": []}
            await emit_craft_panel(
                stream,
                craft_id,
                url,
                product,
                competitive,
                screenshot_url=primary_screenshot_url(product),
                baked_summary=(
                    (session_ctx.get("product_profile") or {}).get("summary")
                    or product.get("summary", "")
                ),
                platform=platform,
            )
        except Exception as e:
            logger.warning("Craft panel re-render failed: %s", e)
