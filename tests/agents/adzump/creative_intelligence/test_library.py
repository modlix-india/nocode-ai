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
from app.agents.adzump.creative_intelligence.models import Competitor, Creative, Essence
from app.agents.adzump.creative_intelligence.sources.adlibrary import AdLibraryError
from app.agents.adzump.creative_intelligence.sources.base import SourceFetch


class FakeSource:
    def __init__(self, *, creatives=None, fail=False, exc: Exception | None = None):
        self._creatives = creatives or []
        self._exc = exc if exc is not None else (AdLibraryError("boom") if fail else None)
        self.calls = 0

    async def fetch(self, *, domain, name):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
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
        # ANY source failure serves stale - the vendor error, a transport
        # error, or a bad-JSON body - never a raise out of creatives_for.
        for name, exc in [("vendor", AdLibraryError("boom")),
                          ("transport", ConnectionError("reset")),
                          ("bad json", ValueError("not json"))]:
            with self.subTest(case=name):
                src = FakeSource(exc=exc)
                rec = self._run(stored=_stale_record(), source=src)
                self.assertEqual(src.calls, 1)
                self.assertIsNotNone(rec)
                self.assertEqual(rec.creatives[0].creative_id, "old")

    def test_empty_fetch_marks_status_empty(self):
        src = FakeSource(creatives=[])
        rec = self._run(stored=None, source=src)
        self.assertEqual(rec.fetch_status, "empty")

    def test_empty_fetch_keeps_prior_record(self):
        # A transiently-empty search result must not overwrite a good record
        # (shared store: zero creatives for a full TTL, essences destroyed).
        src = FakeSource(creatives=[])
        rec = self._run(stored=_stale_record(), source=src)
        self.assertEqual(rec.creatives[0].creative_id, "old")
        library.store.upsert_competitor.assert_not_awaited()

    def test_one_failed_competitor_does_not_abort_the_batch(self):
        good = _fresh_record()

        async def one_bad(*, key, name, ctx, force=False, source=None, enrich=None):
            if key == "bad.com":
                raise RuntimeError("poisoned record")
            return good

        with mock.patch.object(library, "creatives_for", new=one_bad):
            results = asyncio.run(library.creatives_for_all(
                [{"name": "Bad", "url": "https://bad.com"},
                 {"name": "Nike", "url": "https://nike.com"}], ctx={}))
        self.assertEqual(list(results), ["nike.com"])

    def test_no_key_returns_none(self):
        rec = asyncio.run(library.creatives_for(key="", name="x", ctx={}, source=FakeSource()))
        self.assertIsNone(rec)


class FakeEnrich:
    """Records the content_hashes it was asked about; returns a fixed map."""

    def __init__(self, essences=None, fail=False):
        self._essences = essences or {}
        self._fail = fail
        self.calls: list[list[str]] = []

    async def __call__(self, images):
        self.calls.append(sorted(ci.creative.content_hash for ci in images))
        if self._fail:
            raise RuntimeError("boom")
        return self._essences


def _ad(creative_id: str) -> Creative:
    return Creative(creative_id=creative_id, media_type="image",
                    source_asset_url=f"https://vendor/{creative_id}.jpg")


def _rehost_hashing_by_creative_id():
    """Fake rehost: content_hash = the creative_id, bytes returned like the
    perceptual path does."""
    async def fake(src, kind, ctx, hints=None, name="", perceptual=False):
        h = src.rsplit("/", 1)[-1].removesuffix(".jpg")
        return {"url": f"https://files/{h}.jpg", "contentHash": h,
                "perceptualHash": "", "imageBytes": b"IMG-" + h.encode(),
                "contentType": "image/jpeg"}
    return fake


class EnrichIngestTests(unittest.TestCase):
    """Tier-3 wiring: enrich sees only survivors that lack essence, results and
    the carried-forward cache land in the ONE upserted record, and no failure
    mode (hook raise, source failure, cache hit) ever reaches vision."""

    def setUp(self):
        p = mock.patch.object(library._uploads, "rehost_image",
                              new=mock.AsyncMock(side_effect=_rehost_hashing_by_creative_id()))
        p.start(); self.addCleanup(p.stop)
        u = mock.patch.object(library.store, "upsert_competitor",
                              new=mock.AsyncMock(return_value="id1"))
        u.start(); self.addCleanup(u.stop)

    def _run(self, *, stored, source, enrich):
        with mock.patch.object(library.store, "get_competitor",
                               new=mock.AsyncMock(return_value=stored)):
            return asyncio.run(library.creatives_for(
                key="nike.com", name="Nike", ctx={}, source=source, enrich=enrich))

    def test_enrich_runs_on_survivors_and_attaches_before_the_one_write(self):
        enrich = FakeEnrich(essences={"a1": Essence(angle="lakeside living")})
        rec = self._run(stored=None, enrich=enrich,
                        source=FakeSource(creatives=[_ad("a1"), _ad("b2")]))
        self.assertEqual(enrich.calls, [["a1", "b2"]])
        by_id = {c.creative_id: c for c in rec.creatives}
        self.assertEqual(by_id["a1"].essence.angle, "lakeside living")
        self.assertIsNone(by_id["b2"].essence)  # absent verdict = None, not invented
        written = library.store.upsert_competitor.await_args.args[0]
        self.assertEqual(written.creatives[0].essence.angle, "lakeside living")

    def test_cached_essence_carries_forward_and_skips_vision(self):
        stale = _stale_record()
        stale.creatives = [Creative(creative_id="old", content_hash="a1",
                                    essence=Essence(angle="cached"))]
        enrich = FakeEnrich(essences={"b2": Essence(angle="fresh")})
        rec = self._run(stored=stale, enrich=enrich,
                        source=FakeSource(creatives=[_ad("a1"), _ad("b2")]))
        self.assertEqual(enrich.calls, [["b2"]])  # a1 came from the cache
        by_id = {c.creative_id: c for c in rec.creatives}
        self.assertEqual(by_id["a1"].essence.angle, "cached")
        self.assertEqual(by_id["b2"].essence.angle, "fresh")

    def test_carousel_is_rehosted_hashed_and_essenced(self):
        # Carousel/collection assets are images: rehost them like image ads so
        # they get durable URLs, dedup hashes, and an essence.
        carousel = Creative(creative_id="c9", media_type="carousel",
                            source_asset_url="https://vendor/c9.jpg")
        enrich = FakeEnrich(essences={"c9": Essence(angle="grid of rooms")})
        rec = self._run(stored=None, enrich=enrich,
                        source=FakeSource(creatives=[carousel]))
        stored = rec.creatives[0]
        self.assertEqual(stored.file_url, "https://files/c9.jpg")
        self.assertEqual(stored.content_hash, "c9")
        self.assertEqual(stored.essence.angle, "grid of rooms")

    def test_no_still_creative_is_stored_with_essence_none(self):
        # A video with no rehostable poster: no bytes -> no hash -> skipped by
        # dedup and vision, stored as-is (the documented skip, not a crash).
        no_still = Creative(creative_id="vid", media_type="video")
        enrich = FakeEnrich(essences={"a1": Essence(angle="x")})
        rec = self._run(stored=None, enrich=enrich,
                        source=FakeSource(creatives=[_ad("a1"), no_still]))
        self.assertEqual(enrich.calls, [["a1"]])
        by_id = {c.creative_id: c for c in rec.creatives}
        self.assertIsNone(by_id["vid"].essence)
        self.assertEqual(by_id["a1"].essence.angle, "x")

    def test_enrich_failure_still_stores_the_record(self):
        rec = self._run(stored=None, enrich=FakeEnrich(fail=True),
                        source=FakeSource(creatives=[_ad("a1")]))
        self.assertIsNone(rec.creatives[0].essence)
        library.store.upsert_competitor.assert_awaited()

    def test_no_vision_on_cache_hit_source_failure_or_empty_fetch(self):
        cases = [
            ("fresh hit", _fresh_record(), FakeSource(creatives=[_ad("a1")])),
            ("source failure", _stale_record(), FakeSource(fail=True)),
            ("empty fetch", None, FakeSource(creatives=[])),
        ]
        for name, stored, source in cases:
            with self.subTest(case=name):
                enrich = FakeEnrich()
                self._run(stored=stored, source=source, enrich=enrich)
                self.assertEqual(enrich.calls, [])


if __name__ == "__main__":
    unittest.main()
