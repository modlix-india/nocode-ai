"""Unit: creative_intelligence/store.py - pure key + freshness logic.

competitor_key normalizes hosts to one dedup key; is_stale reads a real field on
the typed Competitor (no shape-guessing) and treats missing/unparseable timestamps
as stale (safer to refetch).
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.agents.adzump.creative_intelligence.models import Competitor
from app.agents.adzump.creative_intelligence import store


class CompetitorKeyTests(unittest.TestCase):
    def test_normalization_table(self):
        cases = [
            ("https://www.Nike.com/air", "nike.com"),
            ("nike.com", "nike.com"),
            ("http://uk.gymshark.com/", "uk.gymshark.com"),
            ("WWW.Example.COM", "example.com"),
            ("", ""),
            ("   ", ""),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(store.competitor_key(raw), expected)


class IsStaleTests(unittest.TestCase):
    def _comp(self, fetched_at: str) -> Competitor:
        return Competitor(competitor_key="x.com", last_fetched_at=fetched_at)

    def test_missing_record_is_stale(self):
        self.assertTrue(store.is_stale(None))

    def test_no_timestamp_is_stale(self):
        self.assertTrue(store.is_stale(self._comp("")))

    def test_unparseable_timestamp_is_stale(self):
        self.assertTrue(store.is_stale(self._comp("not-a-date")))

    def test_fresh_and_old_boundary(self):
        now = datetime.now(timezone.utc)
        fresh = (now - timedelta(days=1)).isoformat()
        old = (now - timedelta(days=99)).isoformat()
        self.assertFalse(store.is_stale(self._comp(fresh), max_age_days=30))
        self.assertTrue(store.is_stale(self._comp(old), max_age_days=30))

    def test_naive_timestamp_is_handled(self):
        naive = datetime.now().replace(tzinfo=None).isoformat()
        # naive recent time should not raise and should read as fresh
        self.assertFalse(store.is_stale(self._comp(naive), max_age_days=30))


if __name__ == "__main__":
    unittest.main()
