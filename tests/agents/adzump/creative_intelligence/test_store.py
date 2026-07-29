"""Unit: creative_intelligence/store.py - key normalization, freshness, poisoned reads.

competitor_key normalizes hosts to one dedup key; is_stale treats missing or
unparseable timestamps as stale (safer to refetch) and gives empty records a
short retry window; a stored record that no longer validates reads as an
overwritable miss instead of permanently poisoning its key.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from app.agents.adzump.creative_intelligence.models import Competitor
from app.agents.adzump.creative_intelligence import store


def _comp(fetched_at: str, **fields) -> Competitor:
    return Competitor(competitor_key="x.com", last_fetched_at=fetched_at, **fields)


class StoreTests(unittest.TestCase):
    def test_key_freshness_and_poisoned_read(self):
        for raw, expected in [
            ("https://www.Nike.com/air", "nike.com"),
            ("nike.com", "nike.com"),
            ("http://uk.gymshark.com/", "uk.gymshark.com"),
            ("WWW.Example.COM", "example.com"),
            ("", ""),
            ("   ", ""),
        ]:
            with self.subTest(key=raw or repr(raw)):
                self.assertEqual(store.competitor_key(raw), expected)

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
                self.assertEqual(store.is_stale(record, max_age_days=30), stale)

        with self.subTest("invalid stored record reads as an overwritable miss"):
            bad = {"_id": "rec1", "competitorKey": "x.com", "creatives": "not-a-list"}
            result = SimpleNamespace(success=True, error=None,
                                     data={"result": {"result": {"content": [bad]}}})
            client = SimpleNamespace(post=mock.AsyncMock(return_value=result))
            with mock.patch.object(store, "get_saas_client", return_value=client):
                competitor, record_id = asyncio.run(store._read("x.com", {}))
            self.assertIsNone(competitor)
            self.assertEqual(record_id, "rec1")  # kept so upsert can overwrite


if __name__ == "__main__":
    unittest.main()
