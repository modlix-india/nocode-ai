"""Unit: creative_intelligence/library.py - the cache-or-fetch-or-stale policy.

Drives the whole policy with a fake source (no network) and a patched store:
a fresh hit never touches the source; a miss/stale fetches + upserts; a source
failure serves whatever stale record we had rather than raising.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.agents.adzump.creative_intelligence import library
from app.agents.adzump.creative_intelligence.models import Competitor, Creative
from app.agents.adzump.creative_intelligence.sources.adlibrary import AdLibraryError
from app.agents.adzump.creative_intelligence.sources.base import SourceFetch


class FakeSource:
    def __init__(self, *, creatives=None, fail=False):
        self._creatives = creatives or []
        self._fail = fail
        self.calls = 0

    async def fetch(self, *, domain, name):
        self.calls += 1
        if self._fail:
            raise AdLibraryError("boom")
        return SourceFetch(creatives=self._creatives, resolved_name=name)


def _fresh_record() -> Competitor:
    return Competitor(competitor_key="nike.com", name="Nike",
                      last_fetched_at=datetime.now(timezone.utc).isoformat(),
                      creatives=[Creative(creative_id="old")])


def _stale_record() -> Competitor:
    old = (datetime.now(timezone.utc) - timedelta(days=99)).isoformat()
    return Competitor(competitor_key="nike.com", name="Nike", last_fetched_at=old,
                      creatives=[Creative(creative_id="old")])


class CreativesForPolicyTests(unittest.TestCase):
    def setUp(self):
        # no binary rehosting in unit tests
        p = mock.patch.object(library._uploads, "rehost_image",
                              new=mock.AsyncMock(return_value=None))
        p.start(); self.addCleanup(p.stop)
        self.upsert = mock.patch.object(library.store, "upsert_competitor",
                                        new=mock.AsyncMock(return_value="id1"))
        self.upsert.start(); self.addCleanup(self.upsert.stop)

    def _run(self, *, stored, source):
        with mock.patch.object(library.store, "get_competitor",
                               new=mock.AsyncMock(return_value=stored)):
            return asyncio.run(library.creatives_for(
                key="nike.com", name="Nike", ctx={}, source=source))

    def test_fresh_hit_does_not_call_source(self):
        src = FakeSource(creatives=[Creative(creative_id="new")])
        rec = self._run(stored=_fresh_record(), source=src)
        self.assertEqual(src.calls, 0)
        self.assertEqual(rec.creatives[0].creative_id, "old")

    def test_miss_fetches_and_upserts(self):
        src = FakeSource(creatives=[Creative(creative_id="new", is_active=True)])
        rec = self._run(stored=None, source=src)
        self.assertEqual(src.calls, 1)
        self.assertEqual(rec.creatives[0].creative_id, "new")
        self.assertEqual(rec.fetch_status, "ok")
        library.store.upsert_competitor.assert_awaited()

    def test_stale_refetches(self):
        src = FakeSource(creatives=[Creative(creative_id="new")])
        rec = self._run(stored=_stale_record(), source=src)
        self.assertEqual(src.calls, 1)
        self.assertEqual(rec.creatives[0].creative_id, "new")

    def test_source_failure_serves_stale(self):
        src = FakeSource(fail=True)
        rec = self._run(stored=_stale_record(), source=src)
        self.assertEqual(src.calls, 1)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.creatives[0].creative_id, "old")  # the stale record

    def test_empty_fetch_marks_status_empty(self):
        src = FakeSource(creatives=[])
        rec = self._run(stored=None, source=src)
        self.assertEqual(rec.fetch_status, "empty")

    def test_no_key_returns_none(self):
        rec = asyncio.run(library.creatives_for(key="", name="x", ctx={}, source=FakeSource()))
        self.assertIsNone(rec)


if __name__ == "__main__":
    unittest.main()
