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
from app.agents.adzump.models import CompetitorProfile


class FakeSource:
    def __init__(self, *, creatives=None, exc: Exception | None = None):
        self._creatives = creatives or []
        self._exc = exc
        self.calls = 0

    async def fetch(self, *, domain, name, country=""):
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

    async def __call__(self, images, **who):
        self.calls.append(sorted(ci.creative.content_hash for ci in images))
        if self._fail:
            raise RuntimeError("boom")
        return self._essences


def _record(*, age_days: int, creatives=None, business_urls=None) -> Competitor:
    fetched = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    return Competitor(competitor_key="nike.com", name="Nike", last_fetched_at=fetched,
                      creatives=creatives or [Creative(creative_id="old")],
                      business_urls=business_urls or [])


def _ad(creative_id: str, media_type: str = "image", *,
        last_seen_days_ago: int = 1, **fields) -> Creative:
    """A fetched creative; recently-seen by default so it clears the essence
    recency gate (which has its own dedicated test)."""
    fields.setdefault("last_seen", (
        datetime.now(timezone.utc) - timedelta(days=last_seen_days_ago)).isoformat())
    return Creative(creative_id=creative_id, media_type=media_type,
                    source_asset_url=f"https://vendor/{creative_id}.jpg", **fields)


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
        v = mock.patch.object(library._uploads, "rehost_video",
                              new=mock.AsyncMock(return_value="https://files/video.mp4"))
        v.start(); self.addCleanup(v.stop)
        u = mock.patch.object(library.store, "upsert_competitor",
                              new=mock.AsyncMock(return_value="id1"))
        u.start(); self.addCleanup(u.stop)

    def test_shared_key_searches_every_name(self):
        # Two entries sharing one domain key: the ad search is name-driven,
        # so BOTH names search and their ads merge - the first entry's name
        # must never silently represent the others (live 2026-09-10).
        calls: list[str] = []

        class RecordingSource(FakeSource):
            async def fetch(self, *, domain, name, country=""):
                calls.append(name)
                return SourceFetch(creatives=[_ad("ad-" + name)], resolved_name=name)

        with mock.patch.object(library.store, "get_competitor",
                               new=mock.AsyncMock(return_value=None)):
            fetched, prior = asyncio.run(library._fetch_stage(
                key="cityville.in", names=["Purva Symphony", "Valmark Cityville"],
                ctx={}, force=False, source=RecordingSource()))
        self.assertEqual(calls, ["Purva Symphony", "Valmark Cityville"])
        self.assertEqual(len(fetched.creatives), 2)
        self.assertIsNone(prior)

    def _run(self, *, stored, source, enrich=None, ctx=None):
        library.store.upsert_competitor.reset_mock()
        with mock.patch.object(library.store, "get_competitor",
                               new=mock.AsyncMock(return_value=stored)):
            return asyncio.run(library.creatives_for(
                key="nike.com", name="Nike", ctx=ctx or {}, source=source, enrich=enrich))

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
        with self.subTest("one failed FETCH does not abort the batch"):
            async def fetch_one_bad(*, key, names, ctx, force, source):
                if key == "bad.com":
                    raise RuntimeError("poisoned record")
                return None, _record(age_days=1)  # cache hit
            with mock.patch.object(library, "_fetch_stage", new=fetch_one_bad):
                results = asyncio.run(library.creatives_for_all(
                    [CompetitorProfile(name="Bad", url="https://bad.com"),
                     CompetitorProfile(name="Nike", url="https://nike.com")], ctx={}))
            self.assertEqual(list(results), ["nike.com"])
        with self.subTest("one failed PROCESS does not abort the batch"):
            async def bad_upsert(competitor, ctx):
                if competitor.competitor_key == "bad.com":
                    raise RuntimeError("write refused")
                return "id1"
            with mock.patch.object(library.store, "get_competitor",
                                   new=mock.AsyncMock(return_value=None)), \
                 mock.patch.object(library.store, "upsert_competitor",
                                   new=mock.AsyncMock(side_effect=bad_upsert)):
                results = asyncio.run(library.creatives_for_all(
                    [CompetitorProfile(name="Bad", url="https://bad.com"),
                     CompetitorProfile(name="Nike", url="https://nike.com")],
                    ctx={}, source=FakeSource(creatives=[_ad("a1")])))
            self.assertEqual(list(results), ["nike.com"])
        with self.subTest("no key -> None"):
            self.assertIsNone(asyncio.run(library.creatives_for(
                key="", name="x", ctx={}, source=FakeSource())))

    def test_business_url_stamping(self):
        ctx = {"session_context": {"product_profile": {"url": "http://www.Springs.com/villas/"}}}
        url = "https://springs.com/villas"
        with self.subTest("ingest stamps the session's product, unioned with prior"):
            stored = _record(age_days=99, business_urls=["https://other.com"])
            rec = self._run(stored=stored, source=FakeSource(creatives=[_ad("new")]), ctx=ctx)
            self.assertEqual(rec.business_urls, ["https://other.com", url])
        with self.subTest("cache hit backfills a missing product with ONE write"):
            rec = self._run(stored=_record(age_days=1), source=FakeSource(), ctx=ctx)
            self.assertEqual(rec.business_urls, [url])
            library.store.upsert_competitor.assert_awaited_once()
        with self.subTest("cache hit with the product already stamped never writes"):
            self._run(stored=_record(age_days=1, business_urls=[url]),
                      source=FakeSource(), ctx=ctx)
            library.store.upsert_competitor.assert_not_awaited()
        with self.subTest("no product in session: no stamp, no write"):
            rec = self._run(stored=_record(age_days=1), source=FakeSource())
            self.assertEqual(rec.business_urls, [])
            library.store.upsert_competitor.assert_not_awaited()
        with self.subTest("a failed stamp write still serves the record"):
            with mock.patch.object(library.store, "upsert_competitor",
                                   new=mock.AsyncMock(side_effect=RuntimeError("refused"))):
                rec = self._run(stored=_record(age_days=1), source=FakeSource(), ctx=ctx)
            self.assertEqual(rec.creatives[0].creative_id, "old")
        with self.subTest("batch cache-hit path stamps too"):
            library.store.upsert_competitor.reset_mock()
            with mock.patch.object(library.store, "get_competitor",
                                   new=mock.AsyncMock(return_value=_record(age_days=1))):
                results = asyncio.run(library.creatives_for_all(
                    [CompetitorProfile(name="Nike", url="https://nike.com")], ctx=ctx))
            self.assertEqual(results["nike.com"].business_urls, [url])
            library.store.upsert_competitor.assert_awaited_once()

    def test_default_source_selection(self):
        rows = [("scrapecreators", library.ScrapeCreatorsSource),
                ("adlibrary", library.AdLibrarySource),
                ("bogus", library.ScrapeCreatorsSource)]  # unknown -> default
        for value, source_cls in rows:
            with self.subTest(value):
                library._default_source_instance = None
                with mock.patch.object(library.settings, "ADS_INTEL_SOURCE", value):
                    self.assertIsInstance(library._default_source(), source_cls)
        library._default_source_instance = None
        self.addCleanup(lambda: setattr(library, "_default_source_instance", None))

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
        with self.subTest("video file rehosts for click-through playback"):
            video = Creative(creative_id="vid", media_type="video",
                             source_asset_url="https://vendor/vid.mp4",
                             poster_source_url="https://vendor/vid.jpg")
            rec = self._run(stored=None, enrich=FakeEnrich(),
                            source=FakeSource(creatives=[video]))
            stored = rec.creatives[0]
            self.assertEqual(stored.file_url, "https://files/video.mp4")
            self.assertEqual(stored.poster_url, "https://files/vid.jpg")
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

    def test_essence_recency_gate(self):
        """Vision is spent only on recently-active creatives: active now, or
        last seen within ESSENCE_RECENCY_DAYS. Stale/undated ads are still
        stored and rendered - they just stay essence=None."""
        enrich = FakeEnrich()
        rec = self._run(stored=None, enrich=enrich, source=FakeSource(creatives=[
            _ad("recent", last_seen_days_ago=5),
            _ad("edge", last_seen_days_ago=library.ESSENCE_RECENCY_DAYS),
            _ad("stale", last_seen_days_ago=120),
            _ad("active-no-date", is_active=True, last_seen=""),
            _ad("undated", last_seen=""),
            _ad("garbage-date", last_seen="not-a-timestamp"),
        ]))
        self.assertEqual(enrich.calls, [["active-no-date", "edge", "recent"]])
        # the ineligible ones survive the ingest, just without essence
        self.assertEqual({c.creative_id for c in rec.creatives},
                         {"recent", "edge", "stale", "active-no-date",
                          "undated", "garbage-date"})

    def test_streaming_and_pipelining(self):
        with self.subTest("on_resolved fires per competitor, cache hits included"):
            delivered: list[str] = []

            async def on_resolved(key, record):
                delivered.append(key)

            async def stage(*, key, names, ctx, force, source):
                if key == "cached.com":
                    return None, _record(age_days=1)  # cache hit
                return SourceFetch(creatives=[_ad("a1")], resolved_name=names[0]), None

            with mock.patch.object(library, "_fetch_stage", new=stage), \
                 mock.patch.object(library.store, "get_competitor",
                                   new=mock.AsyncMock(return_value=None)):
                results = asyncio.run(library.creatives_for_all(
                    [CompetitorProfile(name="Cached", url="https://cached.com"),
                     CompetitorProfile(name="Nike", url="https://nike.com")],
                    ctx={}, on_resolved=on_resolved))
            self.assertEqual(sorted(delivered), ["cached.com", "nike.com"])
            self.assertEqual(sorted(results), ["cached.com", "nike.com"])
        with self.subTest("a failing on_resolved does not poison the batch"):
            async def boom(key, record):
                raise RuntimeError("render died")
            with mock.patch.object(library.store, "get_competitor",
                                   new=mock.AsyncMock(return_value=_record(age_days=1))):
                results = asyncio.run(library.creatives_for_all(
                    [CompetitorProfile(name="Nike", url="https://nike.com")],
                    ctx={}, on_resolved=boom))
            self.assertEqual(list(results), ["nike.com"])
        with self.subTest("competitor N's processing overlaps competitor N+1's fetch"):
            # nike's essence pass blocks until adidas' fetch has happened - only
            # a pipelined creatives_for_all can finish (sequential deadlocks;
            # the wait_for timeout turns that into a failure, not a hang).
            fetched_second = asyncio.Event()

            class GateSource(FakeSource):
                async def fetch(self, *, domain, name, country=""):
                    if domain == "adidas.com":
                        fetched_second.set()
                    return await super().fetch(domain=domain, name=name)

            class GatedEnrich(FakeEnrich):
                async def __call__(self, images, **who):
                    await asyncio.wait_for(fetched_second.wait(), timeout=2)
                    return await super().__call__(images, **who)

            gated = GatedEnrich()

            async def run():
                with mock.patch.object(library.store, "get_competitor",
                                       new=mock.AsyncMock(return_value=None)):
                    return await library.creatives_for_all(
                        [CompetitorProfile(name="Nike", url="https://nike.com"),
                         CompetitorProfile(name="Adidas", url="https://adidas.com")],
                        ctx={}, source=GateSource(creatives=[_ad("a1")]),
                        enrich=gated)
            results = asyncio.run(run())
            self.assertEqual(sorted(results), ["adidas.com", "nike.com"])
            # A sequential creatives_for_all would time nike's enrich out
            # (swallowed by _enrich_essence) - both succeeding proves overlap.
            self.assertEqual(len(gated.calls), 2)

    def test_linkless_competitor_fetches_by_name_key(self):
        # Live 2026-09-08: link-less Nambiar (honest no-URL, pre-launch) was
        # silently dropped from every fetch. It must fetch under a name key,
        # with NO domain sent to the source (attribution runs on name alone).
        self.assertEqual(
            library.competitor_identity(
                CompetitorProfile(name="Nambiar Villas", url=None)),
            ("name:nambiar-villas", "Nambiar Villas"))

        captured: dict = {}

        class DomainCapturingSource(FakeSource):
            async def fetch(self, *, domain, name, country=""):
                captured.update(domain=domain, name=name)
                return await super().fetch(domain=domain, name=name,
                                           country=country)

        with mock.patch.object(library.store, "get_competitor",
                               new=mock.AsyncMock(return_value=None)):
            results = asyncio.run(library.creatives_for_all(
                [CompetitorProfile(name="Nambiar Villas", url=None)], {},
                source=DomainCapturingSource(creatives=[_ad("a1")])))
        self.assertIn("name:nambiar-villas", results)
        self.assertEqual(captured, {"domain": "", "name": "Nambiar Villas"})
        record = results["name:nambiar-villas"]
        self.assertEqual(record.domain, "")  # a name key is not a host

    def test_hung_enrich_times_out_and_ships_without_essence(self):
        # Live 2026-09-08: one vision call never returned and the whole batch
        # gather - tool, turn, spinner - sat open 12+ minutes. A hang must
        # degrade to essence-less creatives within the deadline.
        class HungEnrich:
            async def __call__(self, images, **who):
                await asyncio.sleep(3600)

        with mock.patch.object(library, "_ENRICH_TIMEOUT_SECONDS", 0.05):
            record = self._run(stored=None,
                               source=FakeSource(creatives=[_ad("a1")]),
                               enrich=HungEnrich())
        self.assertEqual(record.creatives[0].creative_id, "a1")
        self.assertIsNone(record.creatives[0].essence)  # shipped, essence-less

    def test_hung_processing_drops_one_competitor_not_the_batch(self):
        class HungEnrich:
            async def __call__(self, images, **who):
                await asyncio.sleep(3600)

        profiles = [
            CompetitorProfile(name="Nike", url="https://nike.com"),
            CompetitorProfile(name="Adidas", url="https://adidas.com"),
        ]
        with mock.patch.object(library, "_ENRICH_TIMEOUT_SECONDS", 3600), \
             mock.patch.object(library, "_PROCESS_TIMEOUT_SECONDS", 0.05), \
             mock.patch.object(library.store, "get_competitor",
                               new=mock.AsyncMock(return_value=None)):
            results = asyncio.run(library.creatives_for_all(
                profiles, {}, source=FakeSource(creatives=[_ad("a1")]),
                enrich=HungEnrich()))
        self.assertEqual(results, {})  # both wedged and DROPPED - gather returned


if __name__ == "__main__":
    unittest.main()
