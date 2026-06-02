"""Campaign spec tool — stores campaign-defining fields in session context.

The LLM calls ``set_campaign_spec`` to record the parameters that define a
campaign (platform, budget, duration, accounts, etc.) into
``session.context["campaign_spec"]``. Storage is:

- **Idempotent** — re-sending unchanged state is a silent no-op.
- **Value-validated** — free-text fields must trace to the user's most recent
  message; account/page ids must have been returned by a fetch tool.
- **Provenance-tracked** — each field write records the turn it was set in
  (``session.context["_spec_set_at"][field]``), so the dynamic context can
  render "just set" vs "set N turns ago" and the LLM can reason about state
  age.
- **Delta-summarized** — when a field overwrites a prior value, the tool
  summary reads ``"platform (was Google Ads → Meta)"`` so historical assistant
  messages carry the transition.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.platform import Platform

logger = logging.getLogger(__name__)


# Post-NFKC folding collapses fullwidth digits + most Unicode dashes to ASCII.
# Remaining stragglers: soft hyphen (U+00AD) and hyphen bullet (U+2043) don't
# fold. U+2212 (minus) folds but kept explicit for belt-and-suspenders.
_ID_FORMATTING_CHARS = re.compile(r"[\s\-\u00AD\u2043\u2212]")


def _normalize_id(v: Any) -> Any:
    """Canonicalize account/page ids: NFKC-fold, strip dashes + whitespace.

    Preserves alphanumerics and underscore so Meta ``act_984...`` stays intact.
    Coerces non-strings (ints from JSON) to str first. ``None`` passes through.
    """
    if v is None:
        return v
    s = unicodedata.normalize("NFKC", str(v))
    return _ID_FORMATTING_CHARS.sub("", s)


# Fields that can be set via this tool.
ALLOWED_FIELDS = {
    "platform",
    "duration",
    "budget",
    "location",
    "parent_account",
    "account",
    "fb_page",
    "ig_page",
    "competitive_analysis_declined",
}

# IDs from Google Ads / Meta — must be traceable to a fetch tool's output
# (via session_ctx["account_names"]).
_ACCOUNT_LIKE_FIELDS = {"parent_account", "account", "fb_page", "ig_page"}

# Free-text fields whose values must be traceable to the user's most recent
# message. Together with _ACCOUNT_LIKE_FIELDS, every allowed field passes
# through one kind of traceability check. `competitive_analysis_declined`
# has its own narrow rule (see _field_traceable).
_USER_TEXT_FIELDS = {
    "platform",
    "duration",
    "budget",
    "location",
    "competitive_analysis_declined",
}


def _last_user_text(context: dict[str, Any]) -> str:
    """Most recent user message as a flat string (handles Anthropic list-content)."""
    session = context.get("_session")
    messages = getattr(session, "messages", None) or []
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif isinstance(block, str):
                    parts.append(block)
            return " ".join(parts).strip()
        return str(content).strip()
    return ""


def _current_turn(context: dict[str, Any]) -> int:
    """Monotonic marker for 'this turn'. Uses session._turn_count when available."""
    session = context.get("_session")
    return int(getattr(session, "_turn_count", 0) or 0)


def _field_traceable(field: str, value: Any, last_user: str, session_ctx: dict) -> bool:
    """Can this field's value be traced to what the user actually said?"""
    v = str(value).strip().lower()
    lu = last_user.strip().lower()
    if not v:
        return False

    if field == "location":
        # 1. Matches scraped/detected location in product_data (trusted source)
        from app.agents.adzump.tools.location import _detected_location

        detected = (
            _detected_location(session_ctx.get("product_data") or {}).strip().lower()
        )
        if detected and (v == detected or v in detected or detected in v):
            return True

        # 2. Map-confirm or manual overrides
        if session_ctx.get("_pending_location_confirm"):
            if (
                lu == "confirm"
                or "location_update" in lu
                or lu.startswith("location confirmed")
            ):
                return True
        if lu and v in lu:
            return True
        tokens = [t for t in v.replace(",", " ").split() if len(t) > 3]
        if tokens and sum(1 for t in tokens[:3] if t in lu) >= min(2, len(tokens)):
            return True
        return False

    # Decline flag — accept "true" (any case) when the user said no/skip.
    if field == "competitive_analysis_declined":
        if v in ("true", "yes", "1") and (
            "no" in lu.split() or lu in ("n", "skip", "no thanks", "no need")
        ):
            return True
        return False

    if lu == v:
        return True
    if v in lu:
        return True
    if field == "platform":
        # Both the LLM-stored value and the user's last message must resolve
        # to the same platform via the shared classifier in `platform.py`.
        # Catches "Google Ads" / "google" / "adwords" → GOOGLE and
        # "Meta" / "facebook" / "instagram" / "fb" / "ig" → META.
        v_platform = Platform.from_value(v)
        if v_platform is not None and Platform.from_value(lu) is v_platform:
            return True
    if field in ("duration", "budget"):
        digits_v = "".join(c for c in v if c.isdigit())
        digits_lu = "".join(c for c in lu if c.isdigit())
        if digits_v and digits_v in digits_lu:
            return True
    return False


async def _set_campaign_spec(
    params: dict[str, Any], context: dict[str, Any]
) -> ToolResult:
    """Store campaign-spec fields in session context."""
    session_ctx = context.get("session_context")
    if session_ctx is None:
        return ToolResult(success=False, error="No session context available.")

    spec = session_ctx.setdefault("campaign_spec", {})
    set_at = session_ctx.setdefault("_spec_set_at", {})

    # Filter: allowed fields, non-empty, value differs from stored. Normalize
    # account-like fields on both sides so display-form echoes ("446-197-2633"
    # vs stored "4461972633") don't leak through as false changes.
    incoming: dict[str, Any] = {}
    for k, v in params.items():
        if k not in ALLOWED_FIELDS or v in (None, ""):
            continue
        new_v = _normalize_id(v) if k in _ACCOUNT_LIKE_FIELDS else v
        current = spec.get(k)
        current_norm = _normalize_id(current) if k in _ACCOUNT_LIKE_FIELDS else current
        if current_norm == new_v:
            continue
        incoming[k] = new_v

    if not incoming:
        any_supplied = any(params.get(k) for k in ALLOWED_FIELDS)
        if not any_supplied:
            return ToolResult(
                success=False,
                error="No valid fields provided. Allowed: "
                + ", ".join(sorted(ALLOWED_FIELDS)),
            )
        return ToolResult(success=True, summary="")

    # Value-validity checks — drop offending fields from the write, but let
    # valid fields in the same call go through. A free-text hallucination on
    # one field shouldn't block a legitimate account pick bundled alongside.
    rejected: list[tuple[str, Any, str]] = []

    # Check 1 — free-text fields must reflect what the user actually said.
    last_user = _last_user_text(context)
    for k, v in list(incoming.items()):
        if k in _USER_TEXT_FIELDS and not _field_traceable(
            k, v, last_user, session_ctx
        ):
            rejected.append((k, v, "not traceable to user's last message"))
            incoming.pop(k)

    # Check 2 — account/page IDs must have come from a fetch tool.
    known_ids = set((session_ctx.get("account_names") or {}).keys())
    for k, v in list(incoming.items()):
        if k in _ACCOUNT_LIKE_FIELDS and str(v) not in known_ids:
            rejected.append((k, v, "not returned by any fetch tool this session"))
            incoming.pop(k)

    # If everything was rejected, return a guidance error. If some valid
    # fields remain, keep going — store them and append the rejection note
    # to the summary so the LLM sees which fields it needs to re-ask for.
    if not incoming:
        pairs = ", ".join(f"{k}={v} ({why})" for k, v, why in rejected)
        preview = (last_user or "").replace("\n", " ")[:120]
        logger.warning(
            "campaign_spec_all_rejected: incoming=%s rejected=%s user_said=%r",
            {k: params.get(k) for k in ALLOWED_FIELDS if params.get(k)},
            rejected,
            preview,
        )
        return ToolResult(
            success=False,
            error=(
                f"Cannot set {pairs}. User's last message: '{preview}'. "
                "Ask the user for the missing pieces, then store what they actually say."
            ),
        )

    # Apply writes. Track provenance + build delta strings for the summary so
    # historical messages carry the transition ("was X → Y") and the dynamic
    # context can render "just set" for fields changed this turn.
    turn = _current_turn(context)
    parts: list[str] = []
    for key, value in incoming.items():
        prior = spec.get(key)
        spec[key] = value
        set_at[key] = turn
        if prior not in (None, "") and str(prior) != str(value):
            parts.append(f"{key} (was {prior} → {value})")
        else:
            parts.append(key)

    # Clear the map-confirm marker once location lands in spec. Belt-and-
    # suspenders: works whether the LLM stores via the pending-aware
    # prescription or the conversational "different city" branch.
    if "location" in incoming:
        session_ctx.pop("_pending_location_confirm", None)
        # Capture lat/lng from the map-confirm callback so we can save them
        # later. Plain "confirm" / typed-city paths just store the address.
        from app.agents.adzump.services.business_storage import parse_location_update

        loc_payload = parse_location_update(last_user)

        display_name = ""
        product = session_ctx.setdefault("product_data", {})
        if product.get("product_name") and product.get("location"):
            display_name = f"{product['product_name']}, {product['location']}"

        if loc_payload:
            session_ctx["_location_meta"] = {
                "address": loc_payload["address"] or str(incoming["location"]),
                "lat": loc_payload["lat"],
                "lng": loc_payload["lng"],
                "displayName": display_name,
            }
        else:
            # Plain confirm / typed-city — resolve coordinates dynamically
            address_str = str(incoming["location"])
            from app.agents.adzump.adapters.google.maps import google_maps_client

            geo_res = await google_maps_client.geocode(address_str)
            if geo_res:
                session_ctx["_location_meta"] = {
                    "address": geo_res["address"] or address_str,
                    "lat": geo_res["lat"],
                    "lng": geo_res["lng"],
                    "displayName": display_name,
                }
            else:
                session_ctx["_location_meta"] = {
                    "address": address_str,
                    "lat": None,
                    "lng": None,
                    "displayName": display_name,
                }

        # Resolve coordinates for target area discovery
        coordinates = None
        if (
            session_ctx["_location_meta"].get("lat") is not None
            and session_ctx["_location_meta"].get("lng") is not None
        ):
            coordinates = {
                "lat": session_ctx["_location_meta"]["lat"],
                "lng": session_ctx["_location_meta"]["lng"],
            }

        # Discover target areas dynamically inside the location confirm callback
        from app.agents.adzump.services.geo import discover_geo_targets

        try:
            targets = await discover_geo_targets(coordinates, product)
            product["target_areas"] = targets

            # Map platform geo targets if platform is already set
            platform_val = spec.get("platform")
            if platform_val:
                try:
                    from app.agents.adzump.services.geo.mapping import PlatformGeoMapper

                    mapper = PlatformGeoMapper(session_ctx, context)
                    product["target_areas"] = await mapper.map_target_areas(
                        targets, platform_val
                    )
                except Exception as ex:
                    logger.exception(
                        "Platform geo-mapping failed in location update: %s", ex
                    )
        except Exception as ex:
            logger.exception("Callback discover_geo_targets failed: %s", ex)

        # Persist coordinates/location/target areas to the database immediately
        from app.agents.adzump.services.business_storage import save_campaign

        await save_campaign(session_ctx, context)

        # Trigger an immediate UI card re-render
        from app.agents.adzump.tools.competitor import _emit_final_craft
        from app.agents.adzump.services.business_storage import resolve_url

        stream = context.get("event_stream")
        craft_id = session_ctx.get("craft_id")
        business_url = resolve_url(session_ctx)

        if stream and craft_id and business_url:
            business = session_ctx.setdefault("product_data", {})
            business["location"] = session_ctx["_location_meta"]["address"]
            competitive = session_ctx.get("competitor_analysis") or {"competitors": []}

            # Prioritize rich streamed summary over concise sub-agent summary
            profile = session_ctx.get("product_profile") or {}
            baked_summary = profile.get("summary") or business.get("summary", "")

            await _emit_final_craft(
                stream,
                craft_id,
                business_url,
                business,
                competitive,
                screenshot_url=business.get("primary_screenshot_url")
                or business.get("screenshot_url"),
                baked_summary=baked_summary,
            )

    # If platform was updated (and location was NOT in incoming, since that's handled above),
    # and target_areas are already present, run the mapper!
    if "platform" in incoming and "location" not in incoming:
        platform_val = spec.get("platform")
        product = session_ctx.setdefault("product_data", {})
        target_areas = product.get("target_areas")
        if platform_val and target_areas:
            try:
                from app.agents.adzump.services.geo.mapping import PlatformGeoMapper

                mapper = PlatformGeoMapper(session_ctx, context)
                product["target_areas"] = await mapper.map_target_areas(
                    target_areas, platform_val
                )

                # Persist campaign and trigger immediate UI card re-render
                from app.agents.adzump.services.business_storage import save_campaign

                await save_campaign(session_ctx, context)

                from app.agents.adzump.tools.competitor import _emit_final_craft
                from app.agents.adzump.services.business_storage import resolve_url

                stream = context.get("event_stream")
                craft_id = session_ctx.get("craft_id")
                business_url = resolve_url(session_ctx)
                if stream and craft_id and business_url:
                    if session_ctx.get("_location_meta", {}).get("address"):
                        product["location"] = session_ctx["_location_meta"]["address"]
                    competitive = session_ctx.get("competitor_analysis") or {
                        "competitors": []
                    }
                    profile = session_ctx.get("product_profile") or {}
                    baked_summary = profile.get("summary") or product.get("summary", "")

                    await _emit_final_craft(
                        stream,
                        craft_id,
                        business_url,
                        product,
                        competitive,
                        screenshot_url=product.get("primary_screenshot_url")
                        or product.get("screenshot_url"),
                        baked_summary=baked_summary,
                    )
            except Exception as ex:
                logger.exception(
                    "Platform geo-mapping failed in platform update: %s", ex
                )

    # Are we DONE after this write? If yes, inject the review prescription
    # into the tool result so the LLM renders the summary on the same turn —
    # the dynamic context computed at start-of-turn doesn't know the field
    # we just stored is set.
    review_hint = _review_hint_if_complete(spec, session_ctx)

    if rejected:
        # Partial-success: needed explicit logging so we can tune the guards
        # over the next week. Shows exactly what the LLM tried to bundle and
        # which subset we kept.
        logger.warning(
            "campaign_spec_partial: stored=%s rejected=%s call=%s user_said=%r",
            list(incoming.keys()),
            rejected,
            {k: params.get(k) for k in ALLOWED_FIELDS if params.get(k)},
            (last_user or "").replace("\n", " ")[:120],
        )
        summary_parts = parts + [f"rejected {k}={v} ({why})" for k, v, why in rejected]
        return ToolResult(
            success=True,
            summary=f"Campaign spec updated: {', '.join(summary_parts)}{review_hint}",
        )

    logger.info("campaign_spec_updated: fields=%s", list(incoming.keys()))
    return ToolResult(
        success=True,
        summary=f"Campaign spec updated: {', '.join(parts)}{review_hint}",
    )


def _review_hint_if_complete(spec: dict, session_ctx: dict) -> str:
    """If every required campaign-spec field is now set, return a string that
    instructs the LLM to render the review summary on this same turn.
    Otherwise return ''."""
    platform = Platform.from_value(spec.get("platform"))
    if platform is None:
        return ""
    is_google = platform is Platform.GOOGLE
    is_meta = platform is Platform.META

    # Real-estate? location is required. Otherwise location is optional.
    business_type = (
        (session_ctx.get("product_data") or {}).get("business_type") or ""
    ).lower()
    is_real_estate = any(
        kw in business_type
        for kw in (
            "real estate",
            "realty",
            "villa",
            "apartment",
            "residential",
            "property",
            "housing",
            "homes",
            "realtor",
            "township",
            "builder",
            "developer",
        )
    )
    from app.agents.adzump.services.geo import get_business_scope

    scope = get_business_scope(session_ctx.get("product_data") or {})
    requires_location = is_real_estate or scope in ("hyperlocal", "local", "regional")
    if requires_location and not spec.get("location"):
        return ""

    if not (
        spec.get("duration")
        and spec.get("budget")
        and spec.get("parent_account")
        and spec.get("account")
    ):
        return ""

    # Google: competitive analysis must have been attempted OR declined.
    if is_google:
        if (
            session_ctx.get("competitor_analysis") is None
            and "competitive_analysis_declined" not in spec
        ):
            return ""

    # Meta: fb_page + ig_page required.
    if is_meta and not (spec.get("fb_page") and spec.get("ig_page")):
        return ""

    meta_extra = (
        "\n  - **Facebook Page**: <copy verbatim from State, including '(ID: …)'>"
        "\n  - **Instagram Account**: <copy verbatim from State, including '(ID: …)'>"
        if is_meta
        else ""
    )
    return (
        "\n\nALL CAMPAIGN FIELDS ARE NOW SET. Do NOT call any other tool yet. "
        "Render this exact markdown summary on this turn — copy values VERBATIM "
        "from the `## State` block in the system prompt (do not rephrase, do "
        "not drop fields, do not replace IDs with placeholders like 'Linked' "
        "or 'Connected'):\n\n"
        "Here's your campaign summary:\n\n"
        "  - **Product**: <product name from State>\n"
        "  - **Website**: <website URL from State>\n"
        "  - **Location**: <location from State>\n"
        "  - **Platform**: <platform from State>\n"
        "  - **Duration**: <duration from State>\n"
        "  - **Daily Budget**: <budget from State>\n"
        "  - **Manager / Business Account**: <copy verbatim from State, including '(ID: …)'>\n"
        "  - **Ad Account**: <copy verbatim from State, including '(ID: …)'>"
        f"{meta_extra}\n"
        "  - **Competitors**: <comma-separated names from State, or 'none "
        "analyzed', or 'declined' if competitive_analysis_declined='true'>\n\n"
        'Then call `present_options(question="Ready to launch the campaign?", '
        'options=["Yes, launch", "No, make changes"])`. EVERY bullet must '
        "be present. **On the user's 'Yes, launch' reply, call "
        "`launch_campaign()` (no params) — that's the one tool that persists "
        "the campaign.**"
    )


set_campaign_spec = ToolDefinition(
    name="set_campaign_spec",
    description=(
        "Store campaign-spec fields — the parameters that define the campaign "
        "being built (platform, budget, duration, location, account selection). "
        "Call this whenever the user provides a value via button click, typed "
        "text, or a widget callback. You can set multiple fields in a single "
        "call when the user provides them together in one message."
    ),
    display_name="Update Campaign Spec",
    parameters=[
        ToolParameter(
            name="platform",
            type="string",
            description="Advertising platform: 'Google Ads' or 'Meta'",
            required=False,
            enum=["Google Ads", "Meta"],
        ),
        ToolParameter(
            name="duration",
            type="string",
            description="Campaign duration (e.g., '30 days', '3 months')",
            required=False,
        ),
        ToolParameter(
            name="budget",
            type="string",
            description="Daily budget (e.g., '₹5,000/day')",
            required=False,
        ),
        ToolParameter(
            name="location",
            type="string",
            description="Confirmed project location for real-estate campaigns (city/area).",
            required=False,
        ),
        ToolParameter(
            name="parent_account",
            type="string",
            description=(
                "Selected parent account id — Google Ads manager (MCC) customer_id "
                "or Meta Business id, depending on platform."
            ),
            required=False,
        ),
        ToolParameter(
            name="account",
            type="string",
            description=(
                "Selected final ad account id — child of parent_account. "
                "Google Ads customer_id or Meta ad account id."
            ),
            required=False,
        ),
        ToolParameter(
            name="fb_page",
            type="string",
            description="Meta only — Facebook page id the campaign will post from.",
            required=False,
        ),
        ToolParameter(
            name="ig_page",
            type="string",
            description="Meta only — Instagram Business account id linked to the fb_page.",
            required=False,
        ),
    ],
    execute=_set_campaign_spec,
)

CAMPAIGN_SPEC_TOOLS = [set_campaign_spec]
