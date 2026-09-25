"""When a stored competitor record is too old to serve - the library's cache
policy, applied on every read."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import settings
from app.agents.adzump.creative_intelligence.models import Competitor

DEFAULT_FRESHNESS_DAYS = 30
# An "empty" record is a retry-soon marker, not a month of "this brand runs no
# ads" - keyword search results are noisy, so refetch empties the next day.
EMPTY_RECORD_FRESHNESS_DAYS = 1


def is_stale(competitor: Competitor | None, max_age_days: int | None = None) -> bool:
    """True when the record is missing or its ``last_fetched_at`` is older than the
    freshness window. An unparseable or absent timestamp counts as stale."""
    if competitor is None or not competitor.last_fetched_at:
        return True
    try:
        ts = datetime.fromisoformat(competitor.last_fetched_at)
    except (ValueError, TypeError):
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    days = max_age_days if max_age_days is not None else freshness_days()
    if competitor.fetch_status == "empty":
        days = min(days, EMPTY_RECORD_FRESHNESS_DAYS)
    return datetime.now(timezone.utc) - ts > timedelta(days=days)


def freshness_days() -> int:
    return int(settings.CREATIVE_LIBRARY_FRESHNESS_DAYS or DEFAULT_FRESHNESS_DAYS)
