"""Decide what to send for a launch, and what the answer means.

The request itself is ``adapters.google.campaigns``.

Deliberately NOT a ToolDefinition. Creating a campaign is irreversible and the consent gate
lives in ``launch_campaign``; an LLM-callable tool here could publish without it.

Settings.ADZUMP_PUBLISH_DRY_RUN sends validateOnly instead. On googleAds:mutate that is a
semantic check - it catches bad dates, unknown segments, budgets under the minimum and
manager-account context - so a dry run exercises the whole chain for real. It returns no
resource names, so the response path is the one part it cannot prove.
"""

from __future__ import annotations

import logging
import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import NamedTuple

import httpx

from app.agents.adzump.adapters.google import campaigns, custom_audience
from app.agents.adzump.adapters.google.client import (
    GoogleAdsApiError,
    parse_mutate_errors,
)
from app.agents.adzump.agents.campaign.google.audience.constants import (
    BLUEPRINTS_KEY,
    OWNED_MARKER,
    is_pending,
)
from app.agents.adzump.agents.campaign.google.audience.custom_segment import (
    resolve_name,
)
from app.agents.adzump.agents.campaign.google.emitter import MICROS, OPERATIONS
from app.agents.adzump.agents.campaign.models import (
    Channel,
    resolve_channel,
    set_audience,
)
from app.config import settings

logger = logging.getLogger(__name__)

# 255 is Google's campaign-name limit; the budget name appends " budget" to this one, so the
# headroom keeps that inside it too.
_NAME_MAX = 240
_AMOUNT = re.compile(r"(\d[\d,]*)")
_DURATION = re.compile(r"(\d+)\s*(day|week|month|year)", re.IGNORECASE)
_DAYS_PER = {"day": 1, "week": 7, "month": 30, "year": 365}

_DRY_RUN_ONLY_CUSTOM = (
    "The only audience on this campaign is a custom segment, which is created at launch - a "
    "dry run cannot validate it. Set ADZUMP_PUBLISH_DRY_RUN=false to exercise the real path."
)


class PublishOutcome(NamedTuple):
    ok: bool
    message: str
    campaign: str = ""  # resource name; empty on a dry run or a failure
    dry_run: bool = False
    # False when this channel has no emitter yet. Distinct from a failure: the caller
    # continues with the pre-posting behaviour rather than treating it as a broken launch.
    supported: bool = True
    # The request went out and never came back. Google may or may not have committed it, so
    # this is neither a success to record nor a failure to retry - creating is at-most-once.
    uncertain: bool = False


def _budget_micros(budget: str) -> int:
    """Canonical budget ("₹10,000/day") to micros. The per-day minimum is Google's to
    enforce - it is currency- and account-dependent."""
    match = _AMOUNT.search(budget or "")
    if not match:
        raise ValueError(f"no amount in budget {budget!r}")
    return int(match.group(1).replace(",", "")) * MICROS


def _end_date(duration: str, today: date | None = None) -> str:
    """Canonical duration ("30 days") to an end date.

    Only the end is set. A start would have to be "today" in the ACCOUNT's time zone, and if
    that is behind ours Google reads it as a past date and rejects it; unset, Google starts
    the campaign itself.
    """
    match = _DURATION.search(duration or "")
    if not match:
        raise ValueError(f"no duration in {duration!r}")
    days = int(match.group(1)) * _DAYS_PER[match.group(2).lower()]
    # astimezone, not utc: an account ahead of UTC would get an end date a day early.
    start = today or datetime.now().astimezone().date()
    return (start + timedelta(days=days)).isoformat()


def _geo_targets(product: dict) -> list[str]:
    """The geo constants the location agent already resolved, deduped in panel order.

    Empty means the campaign serves worldwide, so publish refuses rather than emitting it.
    """
    seen: list[str] = []
    for area in product.get("target_areas") or []:
        rn = str((area.get("google") or {}).get("resourceName") or "").strip()
        if rn and rn not in seen:
            seen.append(rn)
    return seen


def _campaign_name(product: dict, channel: Channel) -> str:
    """Names must be unique per account, so a relaunch cannot reuse one.

    The PRODUCT name is what gets trimmed, never the stamp - truncating the whole string
    would cut the stamp off a long name and make every relaunch collide. The budget name
    appends to this, so the budget is what the headroom is for.
    """
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    label = channel.value.replace("_", " ").title()
    tail = f" · {label} · {stamp}"
    product_name = str(product.get("product_name") or "Campaign").strip()
    return product_name[: _NAME_MAX - len(tail)] + tail


async def _materialise_segments(
    session_ctx: dict, block: dict, context: dict, *, dry_run: bool
) -> tuple[dict, list[str], str]:
    """Create each approved custom segment and swap its pending ref for the real one.

    Returns (the block to build from, labels a dry run skipped, error). A CustomAudience
    cannot go inside the atomic mutate, so this is a separate call before it.
    """
    audience_dump = block.get("audience") or {}
    signals = audience_dump.get("signals") or []
    pending = [s for s in signals if is_pending(s.get("ref", ""))]
    if not pending:
        return block, [], ""

    if dry_run:
        # Dropped from a COPY: a dry run must not delete the user's segment from the panel.
        dropped = {s["ref"] for s in pending}
        trimmed = deepcopy(block)
        trimmed_audience = trimmed["audience"]
        trimmed_audience["signals"] = [
            s for s in trimmed_audience.get("signals") or [] if s["ref"] not in dropped
        ]
        groups = [
            [r for r in g if r not in dropped]
            for g in trimmed_audience.get("dimension_groups") or []
        ]
        trimmed_audience["dimension_groups"] = groups
        if not any(groups):
            # What is left would validate on demographics alone - a pass that says nothing
            # about the campaign the user built.
            return block, [], _DRY_RUN_ONLY_CUSTOM
        return trimmed, [s.get("label", "") for s in pending], ""

    spec = session_ctx.get("campaign_spec") or {}
    account = {
        "customer_id": str(spec.get("account") or "").strip(),
        "login_customer_id": str(spec.get("parent_account") or "").strip(),
        "client_code": context.get("client_code", ""),
        "auth_headers": context.get("headers", {}),
    }
    blueprints = session_ctx.get(BLUEPRINTS_KEY) or {}
    for signal in pending:
        # Read before the swap below rewrites it in place.
        ref = signal["ref"]
        plan = blueprints.get(ref)
        if not plan:
            logger.error("publish: no blueprint for %s", ref)
            return block, [], "A custom segment was approved but its terms are missing."
        try:
            existing = await custom_audience.list_enabled(**account)
            created = await custom_audience.create(
                name=resolve_name(existing, plan["label"]),
                description=f"{OWNED_MARKER}{(session_ctx.get('product_data') or {}).get('product_name', '')}",
                keywords=[t["keyword"] for t in plan.get("terms") or []],
                urls=plan.get("urls") or [],
                apps=plan.get("apps") or [],
                **account,
            )
        except GoogleAdsApiError as exc:
            detail = "; ".join(parse_mutate_errors(exc.payload)) or str(exc)[:200]
            logger.error("publish: custom segment create failed: %s", detail)
            hint = (
                " Custom segments cannot be created on a manager account - use the client "
                "account."
                if "ACTION_NOT_PERMITTED" in detail
                or "OPERATION_NOT_PERMITTED" in detail
                else ""
            )
            return (
                block,
                [],
                f"Google refused to create the custom segment: {detail}{hint}",
            )
        _swap_ref(audience_dump, ref, created)
        # Saved before the next one is attempted: a session still holding the pending ref
        # would create this segment a second time on the next launch.
        set_audience(session_ctx, audience_dump)
        # Re-keyed, not dropped - the panel keeps showing what the segment is made of.
        blueprints[created] = blueprints.pop(ref)
        logger.info("publish: created custom segment %s", created)
    return block, [], ""


def _swap_ref(audience_dump: dict, old: str, new: str) -> None:
    for s in audience_dump.get("signals") or []:
        if s.get("ref") == old:
            s["ref"] = new
    audience_dump["dimension_groups"] = [
        [new if r == old else r for r in group]
        for group in audience_dump.get("dimension_groups") or []
    ]


async def publish_campaign(session_ctx: dict, context: dict) -> PublishOutcome:
    """Build this channel's operations and post them. One request, all or nothing."""
    spec = session_ctx.get("campaign_spec") or {}
    product = session_ctx.get("product_data") or {}
    channel = resolve_channel(spec)

    build_operations = OPERATIONS.get(channel)
    if build_operations is None:
        return PublishOutcome(
            False,
            f"{channel.value} campaigns cannot be created from here yet.",
            supported=False,
        )

    from app.agents.adzump.agents.campaign.models import build_dump

    build = build_dump(session_ctx) or {}
    block = build.get(channel.name.lower()) or {}
    if not block:
        return PublishOutcome(False, "Nothing has been built for this campaign yet.")

    customer_id = str(spec.get("account") or "").strip()
    if not customer_id:
        return PublishOutcome(False, "No ad account selected.")

    dry_run = settings.ADZUMP_PUBLISH_DRY_RUN
    block, skipped, error = await _materialise_segments(
        session_ctx, block, context, dry_run=dry_run
    )
    if error:
        return PublishOutcome(False, error)

    try:
        operations = build_operations(
            customer_id=customer_id,
            campaign_name=_campaign_name(product, channel),
            budget_micros=_budget_micros(str(spec.get("budget") or "")),
            build=block,
            product_name=str(product.get("product_name") or ""),
            end_date=_end_date(str(spec.get("duration") or "")),
            geo_targets=_geo_targets(product),
        )
    except (ValueError, KeyError) as exc:
        logger.warning("publish payload build failed: %s", exc)
        return PublishOutcome(False, f"Could not assemble the campaign: {exc}")

    try:
        response = await campaigns.mutate(
            customer_id=customer_id,
            operations=operations,
            validate_only=dry_run,
            login_customer_id=str(spec.get("parent_account") or "").strip(),
            client_code=context.get("client_code", ""),
            auth_headers=context.get("headers", {}),
        )
    except GoogleAdsApiError as exc:
        # exc's message is Google's generic "Request contains an invalid argument"; the
        # cause is in the body, in one of two envelopes.
        errors = parse_mutate_errors(exc.payload)
        detail = errors[0] if errors else str(exc)[:200]
        logger.warning("publish failed: %s", detail)
        return PublishOutcome(False, f"Google rejected the campaign: {detail}")
    except httpx.TimeoutException as exc:
        # A dry run commits nothing, so a timeout there is safely retryable. A live one may
        # already have created the campaign; retrying would make a second.
        logger.error("publish timed out: dry_run=%s %s", dry_run, str(exc)[:120])
        if dry_run:
            return PublishOutcome(False, "The validation request to Google timed out.")
        return PublishOutcome(
            False,
            "The request to Google timed out. The campaign MAY have been created - check "
            "the Google Ads account before trying again.",
            uncertain=True,
        )

    if errors := parse_mutate_errors(response):
        # The FIRST error is the cause; the rest are usually RESOURCE_NOT_FOUND fallout from
        # the operation that actually failed.
        return PublishOutcome(False, f"Google rejected the campaign: {errors[0]}")

    if dry_run:
        logger.info("publish dry run OK: %d operations", len(operations))
        note = (
            f" The custom segment ({', '.join(skipped)}) was left out - it is only created "
            "on a real launch, so this run could not validate it."
            if skipped
            else ""
        )
        return PublishOutcome(
            True,
            f"Validated {len(operations)} operations with Google - nothing was created "
            f"(ADZUMP_PUBLISH_DRY_RUN).{note}",
            dry_run=True,
        )

    campaign = campaigns.created_campaign(response)
    logger.info("campaign created: %s", campaign)
    return PublishOutcome(True, "Campaign created, paused.", campaign=campaign)
