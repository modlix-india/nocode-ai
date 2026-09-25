"""Unit: creative_intelligence/freshness.py - when a stored record is stale.

Missing or unparseable timestamps are stale (safer to refetch); empty records
get a short retry window instead of the full freshness window.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.agents.adzump.creative_intelligence import freshness
from app.agents.adzump.creative_intelligence.models import Competitor


def _comp(fetched_at: str, **fields) -> Competitor:
    return Competitor(competitor_key="x.com", last_fetched_at=fetched_at, **fields)


class FreshnessTests(unittest.TestCase):
    def test_is_stale(self):
        now = datetime.now(timezone.utc)
        fresh = (now - timedelta(days=1)).isoformat()
        two_days = (now - timedelta(days=2)).isoformat()
        old = (now - timedelta(days=99)).isoformat()
        naive = datetime.now().replace(tzinfo=None).isoformat()
        for name, record, stale in [
            ("missing record", None, True),
            ("no timestamp", _comp(""), True),
            ("unparseable timestamp", _comp("not-a-date"), True),
            ("fresh", _comp(fresh), False),
            ("old", _comp(old), True),
            ("naive recent timestamp reads fresh, no raise", _comp(naive), False),
            # fetch_status="empty" is a retry-soon marker: stale after
            # EMPTY_RECORD_FRESHNESS_DAYS, not the full freshness window.
            ("empty record past the short window", _comp(two_days, fetch_status="empty"), True),
            ("non-empty record at the same age", _comp(two_days), False),
        ]:
            with self.subTest(stale=name):
                self.assertEqual(freshness.is_stale(record, max_age_days=30), stale)


if __name__ == "__main__":
    unittest.main()
