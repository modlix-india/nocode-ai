"""Unit: creative_intelligence/library.py - cache-or-fetch-or-stale + essence ingest.

Drives the whole policy with a fake source (no network) and a patched store:
a fresh hit never touches the source; a miss/stale fetches + upserts exactly
once; any failure serves stale rather than raising; and the tool-injected
essence hook only ever sees new-hash survivors.
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
    def __init__(self, *, creatives=None, exc: Exception | None = None):
        self._creatives = creatives or []
        self._exc = exc
        self.calls = 0

    async def fetch(self, *, domain, name):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return SourceFetch(creatives=self._creatives, resolved_name=name)


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


def _record(*, age_days: int, creatives=None) -> Competitor:
    fetched = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    return Competitor(competitor_key="nike.com", name="Nike", last_fetched_at=fetched,
                      creatives=creatives or [Creative(creative_id="old")])


def _ad(creative_id: str, media_type: str = "image") -> Creative:
    return Creative(creative_id=creative_id, media_type=media_type,
                    source_asset_url=f"https://vendor/{creative_id}.jpg")


async def _rehost_hashing_by_creative_id(src, kind, ctx, hints=None, name="", perceptual=False):
    """Fake rehost: content_hash = the creative_id, bytes returned like the real path."""
    h = src.rsplit("/", 1)[-1].removesuffix(".jpg")
    return {"url": f"https://files/{h}.jpg", "contentHash": h, "perceptualHash": "",
            "imageBytes": b"IMG-" + h.encode(), "contentType": "image/jpeg"}


class LibraryTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(library._uploads, "rehost_image",
                              new=mock.AsyncMock(side_effect=_rehost_hashing_by_creative_id))
        p.start(); self.addCleanup(p.stop)
        u = mock.patch.object(library.store, "upsert_competitor",
                              new=mock.AsyncMock(return_value="id1"))
        u.start(); self.addCleanup(u.stop)

    def _run(self, *, stored, source, enrich=None):
        library.store.upsert_competitor.reset_mock()
        with mock.patch.object(library.store, "get_competitor",
                               new=mock.AsyncMock(return_value=stored)):
            return asyncio.run(library.creatives_for(
                key="nike.com", name="Nike", ctx={}, source=source, enrich=enrich))

    def test_cache_or_fetch_or_stale_policy(self):
        with self.subTest("fresh hit never calls the source"):
            src = FakeSource(creatives=[_ad("new")])
            rec = self._run(stored=_record(age_days=1), source=src)
            self.assertEqual(src.calls, 0)
            self.assertEqual(rec.creatives[0].creative_id, "old")
        with self.subTest("miss fetches + upserts; stale refetches"):
            for stored in (None, _record(age_days=99)):
                rec = self._run(stored=stored, source=FakeSource(creatives=[_ad("new")]))
                self.assertEqual(rec.creatives[0].creative_id, "new")
                self.assertEqual(rec.fetch_status, "ok")
                library.store.upsert_competitor.assert_awaited()
        # ANY source failure serves stale - never a raise out of creatives_for.
        for name, exc in [("vendor", AdLibraryError("boom")),
                          ("transport", ConnectionError("reset")),
                          ("bad json", ValueError("not json"))]:
            with self.subTest(failure=name):
                rec = self._run(stored=_record(age_days=99), source=FakeSource(exc=exc))
                self.assertEqual(rec.creatives[0].creative_id, "old")
        with self.subTest("empty fetch on a miss stores a retry-soon empty record"):
            rec = self._run(stored=None, source=FakeSource(creatives=[]))
            self.assertEqual(rec.fetch_status, "empty")
        with self.subTest("empty fetch NEVER overwrites a record with creatives"):
            rec = self._run(stored=_record(age_days=99), source=FakeSource(creatives=[]))
            self.assertEqual(rec.creatives[0].creative_id, "old")
            library.store.upsert_competitor.assert_not_awaited()
        with self.subTest("one failed competitor does not abort the batch"):
            async def one_bad(*, key, name, ctx, force=False, source=None, enrich=None):
                if key == "bad.com":
                    raise RuntimeError("poisoned record")
                return _record(age_days=1)
            with mock.patch.object(library, "creatives_for", new=one_bad):
                results = asyncio.run(library.creatives_for_all(
                    [{"name": "Bad", "url": "https://bad.com"},
                     {"name": "Nike", "url": "https://nike.com"}], ctx={}))
            self.assertEqual(list(results), ["nike.com"])
        with self.subTest("no key -> None"):
            self.assertIsNone(asyncio.run(library.creatives_for(
                key="", name="x", ctx={}, source=FakeSource())))

    def test_essence_ingest_wiring(self):
        with self.subTest("enrich sees only survivors; essences land in the ONE write"):
            enrich = FakeEnrich(essences={"a1": Essence(angle="lakeside living")})
            rec = self._run(stored=None, enrich=enrich,
                            source=FakeSource(creatives=[_ad("a1"), _ad("b2")]))
            self.assertEqual(enrich.calls, [["a1", "b2"]])
            by_id = {c.creative_id: c for c in rec.creatives}
            self.assertEqual(by_id["a1"].essence.angle, "lakeside living")
            self.assertIsNone(by_id["b2"].essence)  # absent verdict = None, not invented
            written = library.store.upsert_competitor.await_args.args[0]
            self.assertEqual(written.creatives[0].essence.angle, "lakeside living")
        with self.subTest("cached essence carries forward and skips vision"):
            stale = _record(age_days=99, creatives=[
                Creative(creative_id="old", content_hash="a1", essence=Essence(angle="cached"))])
            enrich = FakeEnrich(essences={"b2": Essence(angle="fresh")})
            rec = self._run(stored=stale, enrich=enrich,
                            source=FakeSource(creatives=[_ad("a1"), _ad("b2")]))
            self.assertEqual(enrich.calls, [["b2"]])  # a1 came from the cache
            by_id = {c.creative_id: c for c in rec.creatives}
            self.assertEqual((by_id["a1"].essence.angle, by_id["b2"].essence.angle),
                             ("cached", "fresh"))
        with self.subTest("carousel is rehosted, hashed, and essenced like an image"):
            enrich = FakeEnrich(essences={"c9": Essence(angle="grid of rooms")})
            rec = self._run(stored=None, enrich=enrich,
                            source=FakeSource(creatives=[_ad("c9", media_type="carousel")]))
            stored = rec.creatives[0]
            self.assertEqual((stored.file_url, stored.content_hash, stored.essence.angle),
                             ("https://files/c9.jpg", "c9", "grid of rooms"))
        with self.subTest("video with no still: skipped by vision, stored essence=None"):
            enrich = FakeEnrich(essences={"a1": Essence(angle="x")})
            rec = self._run(stored=None, enrich=enrich,
                            source=FakeSource(creatives=[
                                _ad("a1"), Creative(creative_id="vid", media_type="video")]))
            self.assertEqual(enrich.calls, [["a1"]])
            self.assertIsNone({c.creative_id: c for c in rec.creatives}["vid"].essence)
        with self.subTest("enrich failure still stores the record"):
            rec = self._run(stored=None, enrich=FakeEnrich(fail=True),
                            source=FakeSource(creatives=[_ad("a1")]))
            self.assertIsNone(rec.creatives[0].essence)
            library.store.upsert_competitor.assert_awaited()
        for name, stored, source in [
            ("fresh hit", _record(age_days=1), FakeSource(creatives=[_ad("a1")])),
            ("source failure", _record(age_days=99), FakeSource(exc=AdLibraryError("x"))),
            ("empty fetch", None, FakeSource(creatives=[])),
        ]:
            with self.subTest(no_vision_on=name):
                enrich = FakeEnrich()
                self._run(stored=stored, source=source, enrich=enrich)
                self.assertEqual(enrich.calls, [])


if __name__ == "__main__":
    unittest.main()
