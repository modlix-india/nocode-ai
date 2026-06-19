"""Compute campaign conversion signal from Google Ads metrics."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from app.agents.adzump.recommendations.models import (
    ConversionSignal,
    ConversionSignalStatus,
)
from app.agents.adzump.adapters.google.conversion_metrics import (
    conversion_metrics_adapter,
)
from app.agents.adzump.adapters.google.conversion_enums import (
    ConversionTrackingStatus,
    MAX_SPEND_STRATEGIES,
    SIGNAL_DROPPING_THRESHOLD,
    SIGNAL_MAX_SPEND_DROPPING_THRESHOLD,
    SIGNAL_MIN_CAMPAIGN_AGE_DAYS,
    SIGNAL_MIN_PRIOR_VOLUME,
)

logger = logging.getLogger(__name__)


# Computation helpers


def _split_windows(
    rows: list[dict[str, Any]],
) -> tuple[float, float, str | None]:
    """Split sorted rows into prior/recent halves and total conversions."""
    if not rows:
        return 0.0, 0.0, None

    # NOTE: If rows has an odd number of elements, prior_rows and recent_rows
    # will have slightly asymmetric lengths (e.g., 13 vs 14 elements). This split
    # is acceptable for a cheap passive signal, but users should be aware that
    # odd-length lists yield asymmetric comparison windows.
    sorted_rows = sorted(rows, key=lambda r: r.get("date", ""))
    mid = len(sorted_rows) // 2
    prior_rows = sorted_rows[:mid]
    recent_rows = sorted_rows[mid:]

    prior_sum = sum(
        float(r.get("metrics", {}).get("conversions", 0)) for r in prior_rows
    )
    recent_sum = sum(
        float(r.get("metrics", {}).get("conversions", 0)) for r in recent_rows
    )
    most_recent = sorted_rows[-1].get("date") if sorted_rows else None

    return recent_sum, prior_sum, most_recent


def _compute_campaign_age(start_date_str: str | None) -> int | None:
    """Return campaign age in days, or None if the date is invalid."""
    if not start_date_str:
        return None
    try:
        start = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        return (date.today() - start).days
    except ValueError:
        return None


def _compute_signal(
    recent: float,
    prior: float,
    tracking_status: str,
    campaign_age_days: int | None,
    bidding_strategy: str,
) -> ConversionSignal:
    """Derive the conversion signal status from recent/prior metrics."""
    now = datetime.now(timezone.utc).isoformat()

    # untracked account
    if tracking_status == ConversionTrackingStatus.NOT_CONVERSION_TRACKED.value:
        return ConversionSignal(
            status=ConversionSignalStatus.UNTRACKED,
            window="14d_vs_prior_14d",
            min_volume_met=False,
            conversions_recent=recent,
            conversions_prior=prior,
            evaluated_at=now,
        )

    # campaign too young for reliable signal
    if (
        campaign_age_days is not None
        and campaign_age_days < SIGNAL_MIN_CAMPAIGN_AGE_DAYS
    ):
        return ConversionSignal(
            status=ConversionSignalStatus.INSUFFICIENT_HISTORY,
            window="14d_vs_prior_14d",
            min_volume_met=False,
            conversions_recent=recent,
            conversions_prior=prior,
            campaign_age_days=campaign_age_days,
            evaluated_at=now,
        )

    # insufficient prior conversion volume
    if prior < SIGNAL_MIN_PRIOR_VOLUME:
        return ConversionSignal(
            status=ConversionSignalStatus.INSUFFICIENT_HISTORY,
            window="14d_vs_prior_14d",
            min_volume_met=False,
            conversions_recent=recent,
            conversions_prior=prior,
            campaign_age_days=campaign_age_days,
            evaluated_at=now,
        )

    # compare recent vs prior performance
    if bidding_strategy in MAX_SPEND_STRATEGIES:
        effective_threshold = SIGNAL_MAX_SPEND_DROPPING_THRESHOLD
    else:
        effective_threshold = SIGNAL_DROPPING_THRESHOLD

    delta = (recent - prior) / prior if prior > 0 else 0.0
    delta_pct = round(delta * 100, 1)

    if delta < effective_threshold:
        return ConversionSignal(
            status=ConversionSignalStatus.DROPPING,
            delta_pct=delta_pct,
            window="14d_vs_prior_14d",
            min_volume_met=True,
            conversions_recent=recent,
            conversions_prior=prior,
            campaign_age_days=campaign_age_days,
            evaluated_at=now,
        )

    # healthy recent conversion trend
    return ConversionSignal(
        status=ConversionSignalStatus.STABLE,
        delta_pct=delta_pct,
        window="14d_vs_prior_14d",
        min_volume_met=True,
        conversions_recent=recent,
        conversions_prior=prior,
        campaign_age_days=campaign_age_days,
        evaluated_at=now,
    )


# Public compute_signal API for optimize.py bridge


async def compute_signal(
    campaign_id: str,
    customer_id: str,
    login_customer_id: str,
    client_code: str,
    auth_headers: dict,
) -> ConversionSignal | None:
    """Fetch metrics and return a campaign conversion signal model."""
    try:
        data = await conversion_metrics_adapter.fetch_conversion_signal(
            campaign_id=campaign_id,
            customer_id=customer_id,
            login_customer_id=login_customer_id,
            client_code=client_code,
            auth_headers=auth_headers,
        )
    except Exception:
        logger.exception("compute_signal: fetch failed campaign=%s", campaign_id)
        return None

    recent, prior, _ = _split_windows(data["flat_rows"])
    campaign_age = _compute_campaign_age(data["campaign_start_date"])

    signal = _compute_signal(
        recent=recent,
        prior=prior,
        tracking_status=data["tracking_status"],
        campaign_age_days=campaign_age,
        bidding_strategy=data["bidding_strategy"],
    )

    start_date, end_date = data["date_range"]
    logger.info(
        "compute_signal: campaign=%s status=%s delta=%s "
        "recent=%.0f prior=%.0f range=%s to %s",
        campaign_id,
        signal.status,
        f"{signal.delta_pct:+.1f}%" if signal.delta_pct is not None else "n/a",
        signal.conversions_recent,
        signal.conversions_prior,
        start_date,
        end_date,
    )

    return signal
