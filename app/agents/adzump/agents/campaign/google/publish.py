"""Post the assembled build to Google as one atomic mutate.

Deliberately NOT a ToolDefinition. Creating a campaign is irreversible and the consent gate
lives in ``launch_campaign``; an LLM-callable tool here could publish without it.

One request, all or nothing: partialFailure is false, so either the whole campaign exists or
none of it does.

Settings.ADZUMP_PUBLISH_DRY_RUN sends validateOnly instead. On googleAds:mutate that is a
semantic check - it catches bad dates, unknown segments, budgets under the minimum and
manager-account context - so a dry run exercises the whole chain for real. It returns no
resource names, so the response path is the one part it cannot prove.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, NamedTuple

import httpx

from app.agents.adzump.adapters.google.client import (
    GoogleAdsApiError,
    google_ads_client,
)
from app.agents.adzump.agents.campaign.google.emitter import (
    MICROS,
    OPERATIONS,
    parse_mutate_errors,
)
from app.agents.adzump.agents.campaign.models import Channel, resolve_channel
from app.config import settings

logger = logging.getLogger(__name__)

# 255 is Google's campaign-name limit; the budget name appends " budget" to this one, so the
# headroom keeps that inside it too.
_NAME_MAX = 240
_AMOUNT = re.compile(r"(\d[\d,]*)")
_DURATION = re.compile(r"(\d+)\s*(day|week|month|year)", re.I)
_DAYS_PER = {"day": 1, "week": 7, "month": 30, "year": 365}


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
    return ((today or date.today()) + timedelta(days=days)).isoformat()


def _campaign_name(product: dict, channel: Channel) -> str:
    """Names must be unique per account, so a relaunch cannot reuse one.

    The PRODUCT name is what gets trimmed, never the stamp - truncating the whole string
    would cut the stamp off a long name and make every relaunch collide. The budget name
    appends to this, so the budget is what the headroom is for.
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    label = channel.value.replace("_", " ").title()
    tail = f" · {label} · {stamp}"
    product_name = str(product.get("product_name") or "Campaign").strip()
    return product_name[: _NAME_MAX - len(tail)] + tail


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

    try:
        operations = build_operations(
            customer_id=customer_id,
            campaign_name=_campaign_name(product, channel),
            budget_micros=_budget_micros(str(spec.get("budget") or "")),
            build=block,
            product_name=str(product.get("product_name") or ""),
            end_date=_end_date(str(spec.get("duration") or "")),
        )
    except (ValueError, KeyError) as exc:
        logger.warning("publish payload build failed: %s", exc)
        return PublishOutcome(False, f"Could not assemble the campaign: {exc}")

    dry_run = settings.ADZUMP_PUBLISH_DRY_RUN
    payload: dict[str, Any] = {
        "mutateOperations": operations,
        # Sent explicitly though it is the default: with false, "all operations will be
        # carried out in one transaction if and only if they are all valid", so a campaign
        # can never be half created. Too important to leave resting on a default.
        "partialFailure": False,
    }
    if dry_run:
        payload["validateOnly"] = True

    try:
        response = await google_ads_client.post(
            f"customers/{customer_id}/googleAds:mutate",
            payload,
            context.get("client_code", ""),
            context.get("headers", {}),
            str(spec.get("parent_account") or "").strip() or None,
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
        return PublishOutcome(
            True,
            f"Validated {len(operations)} operations with Google - nothing was created "
            "(ADZUMP_PUBLISH_DRY_RUN).",
            dry_run=True,
        )

    campaign = _created_campaign(response)
    logger.info("campaign created: %s", campaign)
    return PublishOutcome(True, "Campaign created, paused.", campaign=campaign)


def _created_campaign(response: dict) -> str:
    """The campaign's resource name out of the mutate response.

    Results come back positionally, one per operation, so this reads the campaign's own
    result rather than assuming an index.
    """
    for result in response.get("mutateOperationResponses") or []:
        if name := (result.get("campaignResult") or {}).get("resourceName"):
            return name
    return ""
