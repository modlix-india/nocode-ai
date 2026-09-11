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

from app.agents.adzump.creative_intelligence import library, taxonomy
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
    """A fetched creative, recently-seen by default."""
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
        # Served-URL verification passes by default here (it has its own
        # dedicated tests in test_verify.py) - these tests own the
        # cache/fetch/essence policy, not asset validity.
        ver = mock.patch(
            "app.agents.adzump.creative_intelligence.verify.verify_creative",
            new=mock.AsyncMock(return_value=(True, "")))
        ver.start(); self.addCleanup(ver.stop)

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
            fetched, prior, searched = asyncio.run(library._fetch_stage(
                key="cityville.in", names=["Purva Symphony", "Valmark Cityville"],
                ctx={}, force=False, source=RecordingSource()))
        self.assertEqual(calls, ["Purva Symphony", "Valmark Cityville"])
        self.assertEqual(len(fetched.creatives), 2)
        self.assertIsNone(prior)
        self.assertEqual(searched, ["Purva Symphony", "Valmark Cityville"])

    def test_all_failed_validation_is_error_not_empty(self):
        # Rule 7: a pipeline failure must never be disguised as "this
        # competitor has no ads" - and the diagnostics say why.
        with mock.patch(
                "app.agents.adzump.creative_intelligence.verify.verify_creative",
                new=mock.AsyncMock(return_value=(False, "fetch_failed"))):
            rec = self._run(stored=None,
                            source=FakeSource(creatives=[_ad("a1"), _ad("b2")]))
        self.assertEqual(rec.fetch_status, "error")
        self.assertIn("fetch_failed=2", rec.fetch_error)
        self.assertEqual(rec.creatives, [])
        self.assertEqual(rec.total_creatives, 0)
        self.assertEqual({d["creativeId"] for d in rec.dropped}, {"a1", "b2"})

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
                return None, _record(age_days=1), []  # cache hit
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
                Creative(creative_id="old", content_hash="a1",
                         essence=Essence(angle="cached",
                                         taxonomy_version=taxonomy.TAXONOMY_VERSION))])
            enrich = FakeEnrich(essences={"b2": Essence(angle="fresh")})
            rec = self._run(stored=stale, enrich=enrich,
                            source=FakeSource(creatives=[_ad("a1"), _ad("b2")]))
            self.assertEqual(enrich.calls, [["b2"]])  # a1 came from the cache
            by_id = {c.creative_id: c for c in rec.creatives}
            self.assertEqual((by_id["a1"].essence.angle, by_id["b2"].essence.angle),
                             ("cached", "fresh"))
            # fresh classifications are stamped with the current vintage
            self.assertEqual(by_id["b2"].essence.taxonomy_version,
                             taxonomy.TAXONOMY_VERSION)
        with self.subTest("stale-taxonomy essence is NOT carried - re-classified"):
            # acceptance 11: a TAXONOMY_VERSION bump re-runs classification on
            # existing records at their next real ingest.
            stale = _record(age_days=99, creatives=[
                Creative(creative_id="old", content_hash="a1",
                         essence=Essence(angle="cached", taxonomy_version="0"))])
            enrich = FakeEnrich(essences={"a1": Essence(angle="reclassified")})
            rec = self._run(stored=stale, enrich=enrich,
                            source=FakeSource(creatives=[_ad("a1")]))
            self.assertEqual(enrich.calls, [["a1"]])
            self.assertEqual(rec.creatives[0].essence.angle, "reclassified")
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

    def test_every_survivor_is_classified(self):
        """No recency filter on the essence pass: the relevance gate needs a
        category on EVERY stored creative, so stale and undated ads are
        classified too (the old 30-day recency gate is gone)."""
        enrich = FakeEnrich()
        rec = self._run(stored=None, enrich=enrich, source=FakeSource(creatives=[
            _ad("recent", last_seen_days_ago=5),
            _ad("stale", last_seen_days_ago=120),
            _ad("undated", last_seen=""),
        ]))
        self.assertEqual(enrich.calls, [["recent", "stale", "undated"]])
        self.assertEqual({c.creative_id for c in rec.creatives},
                         {"recent", "stale", "undated"})

    def test_streaming_and_pipelining(self):
        with self.subTest("on_resolved fires per competitor, cache hits included"):
            delivered: list[str] = []

            async def on_resolved(key, record):
                delivered.append(key)

            async def stage(*, key, names, ctx, force, source):
                if key == "cached.com":
                    return None, _record(age_days=1), []  # cache hit
                return (SourceFetch(creatives=[_ad("a1")], resolved_name=names[0]),
                        None, list(names))

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

    def test_fresh_record_only_answers_for_searched_names(self):
        """A cache hit is name-aware (live 2026-09-11: 'Purva Sparkling
        Springs' resolved to puravankara.com and was served the record built
        under 'Puravankara The Sound of Water' - no search ever ran for it).
        An uncovered name searches just itself and MERGES with the stored
        creatives; covered names stay pure cache hits."""
        def _sound_of_water_record(age_days=1) -> Competitor:
            rec = _record(age_days=age_days, creatives=[
                Creative(creative_id="junk", content_hash="junk",
                         file_url="https://files/junk.jpg",
                         essence=Essence(angle="stored",
                                         taxonomy_version=taxonomy.TAXONOMY_VERSION))])
            rec.name = "Puravankara The Sound of Water"
            rec.searched_names = ["Puravankara The Sound of Water"]
            return rec

        with self.subTest("covered name (legacy record: display name) -> pure hit"):
            legacy = _record(age_days=1)  # name="Nike", no searchedNames
            src = FakeSource(creatives=[_ad("new")])
            rec = self._run(stored=legacy, source=src)
            self.assertEqual(src.calls, 0)
        with self.subTest("uncovered name searches itself and merges"):
            src = FakeSource(creatives=[_ad("pss1")])
            rec = self._run(stored=_sound_of_water_record(), source=src)
            self.assertEqual(src.calls, 1)  # only the missing name searched
            self.assertEqual({c.creative_id for c in rec.creatives},
                             {"pss1", "junk"})  # union, not replacement
            self.assertEqual(rec.searched_names,
                             ["Puravankara The Sound of Water", "Nike"])
            self.assertEqual(rec.creatives[-1].essence.angle, "stored")
            library.store.upsert_competitor.assert_awaited()
        with self.subTest("second visit under the merged name is a pure hit"):
            merged = _sound_of_water_record()
            merged.searched_names.append("Nike")
            src = FakeSource(creatives=[_ad("pss1")])
            self._run(stored=merged, source=src)
            self.assertEqual(src.calls, 0)
        with self.subTest("essence-less prior creative re-classifies from its own asset"):
            # no fresh vendor bytes on an augment - the rehosted file is the
            # source (video would use its poster). Backfill feeds the enrich.
            stored = _sound_of_water_record()
            stored.creatives[0].essence = None

            import httpx

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, content=b"IMG-junk",
                                      headers={"content-type": "image/jpeg"})

            real_client = httpx.AsyncClient

            def make_client(**kw):
                kw.pop("transport", None)
                return real_client(transport=httpx.MockTransport(handler), **kw)

            enrich = FakeEnrich(essences={"junk": Essence(angle="recovered")})
            with mock.patch.object(library.httpx, "AsyncClient", make_client):
                rec = self._run(stored=stored, enrich=enrich,
                                source=FakeSource(creatives=[_ad("pss1")]))
            self.assertIn("junk", enrich.calls[0])
            by_id = {c.creative_id: c for c in rec.creatives}
            self.assertEqual(by_id["junk"].essence.angle, "recovered")
        with self.subTest("failed augment search serves the stored record"):
            from app.agents.adzump.creative_intelligence.sources.adlibrary import (
                AdLibraryError as Err)
            rec = self._run(stored=_sound_of_water_record(),
                            source=FakeSource(exc=Err("boom")))
            self.assertEqual([c.creative_id for c in rec.creatives], ["junk"])
        with self.subTest("full stale refresh RESETS the name claims"):
            # a refresh discards other names' ads - keeping their claims would
            # serve name B the post-refresh record without B ever re-searching.
            src = FakeSource(creatives=[_ad("fresh")])
            rec = self._run(stored=_sound_of_water_record(age_days=99), source=src)
            self.assertEqual(rec.searched_names, ["Nike"])

    def test_relevance_gate(self):
        """Stage C acceptance rows (spec 2026-09-11): only ads whose category
        matches the product's are written; rejections land in dropped[] and an
        all-rejected fetch is empty+emptyReason, never 'runs no ads'."""
        apartment_ctx = {"session_context": {"product_data": {
            "business_type": "Pre-launch high-rise apartments, Whitefield Bangalore",
            "place": {"address": "Whitefield, Bangalore, Karnataka"},
        }}}

        def classified(essences: dict[str, Essence]) -> FakeEnrich:
            return FakeEnrich(essences=essences)

        def apartment(conf=0.9, **kw) -> Essence:
            kw.setdefault("category", "residential_apartment")
            kw.setdefault("category_confidence", conf)
            return Essence(**kw)

        with self.subTest("same category, different developer -> ACCEPTED"):
            rec = self._run(stored=None, ctx=apartment_ctx,
                            enrich=classified({"a1": apartment()}),
                            source=FakeSource(creatives=[_ad("a1")]))
            self.assertEqual(rec.fetch_status, "ok")
            self.assertEqual([c.creative_id for c in rec.creatives], ["a1"])
        rejection_rows = [
            ("office ad", Essence(category="commercial_office",
                                  category_confidence=0.95), "category_mismatch"),
            ("plot ad (different branch)",
             Essence(category="residential_plot", category_confidence=0.95),
             "category_mismatch"),
            ("auto/FMCG ad", Essence(category="other_industry",
                                     category_confidence=0.9), "non_real_estate"),
            ("dog photo, no copy", Essence(category="unknown",
                                           category_confidence=0.2), "unknown_category"),
            ("right category, low confidence", apartment(conf=0.5), "low_confidence"),
            ("right category, wrong city",
             apartment(market="Mumbai / Andheri"), "market_mismatch"),
            ("essence never produced (fail closed)", None, "unknown_category"),
        ]
        for label, essence, want_reason in rejection_rows:
            with self.subTest(label):
                enrich = classified({"a1": essence} if essence else {})
                rec = self._run(stored=None, ctx=apartment_ctx, enrich=enrich,
                                source=FakeSource(creatives=[_ad("a1")]))
                self.assertEqual(rec.creatives, [])
                self.assertEqual(rec.fetch_status, "empty")
                self.assertEqual(rec.empty_reason, want_reason)
                self.assertEqual(rec.dropped[0]["reason"], want_reason)
        with self.subTest("alias city (Bengaluru) still matches"):
            rec = self._run(stored=None, ctx=apartment_ctx,
                            enrich=classified(
                                {"a1": apartment(market="Bengaluru / Whitefield")}),
                            source=FakeSource(creatives=[_ad("a1")]))
            self.assertEqual(rec.fetch_status, "ok")
        with self.subTest("mixed batch: apartments kept, office rejected"):
            rec = self._run(stored=None, ctx=apartment_ctx,
                            enrich=classified({
                                "a1": apartment(),
                                "b2": Essence(category="commercial_office",
                                              category_confidence=0.9),
                                "c3": apartment(),
                            }),
                            source=FakeSource(creatives=[_ad("a1"), _ad("b2"),
                                                         _ad("c3")]))
            self.assertEqual({c.creative_id for c in rec.creatives}, {"a1", "c3"})
            self.assertEqual(rec.fetch_status, "ok")
            self.assertEqual(rec.empty_reason, "")
            self.assertEqual([d["creativeId"] for d in rec.dropped], ["b2"])
        with self.subTest("broker ad is KEPT, only flagged"):
            rec = self._run(stored=None, ctx=apartment_ctx,
                            enrich=classified(
                                {"a1": apartment(advertiser_role="broker")}),
                            source=FakeSource(creatives=[_ad("a1")]))
            self.assertEqual(rec.creatives[0].essence.advertiser_role, "broker")
        with self.subTest("override yardstick (villa) gates without Stage A"):
            ctx = {"session_context": {"product_data": {
                "business_type": "Pre-launch high-rise apartments",
                "product_category_override": "residential_villa",
            }}}
            rec = self._run(stored=None, ctx=ctx,
                            enrich=classified({
                                "a1": Essence(category="residential_villa",
                                              category_confidence=0.9),
                                "b2": apartment(),
                            }),
                            source=FakeSource(creatives=[_ad("a1"), _ad("b2")]))
            self.assertEqual([c.creative_id for c in rec.creatives], ["a1"])
        with self.subTest("unclassifiable product -> gate OFF, nothing rejected"):
            ctx = {"session_context": {"product_data": {
                "business_type": "artisanal candles"}}}
            rec = self._run(stored=None, ctx=ctx, enrich=classified({}),
                            source=FakeSource(creatives=[_ad("a1")]))
            self.assertEqual([c.creative_id for c in rec.creatives], ["a1"])
        with self.subTest("re-run never duplicates dropped[] entries"):
            enrich = classified({"a1": Essence(category="commercial_office",
                                               category_confidence=0.9)})
            first = self._run(stored=None, ctx=apartment_ctx, enrich=enrich,
                              source=FakeSource(creatives=[_ad("a1")]))
            self.assertEqual([d["creativeId"] for d in first.dropped], ["a1"])
            first.last_fetched_at = (  # age it so the second run refetches
                datetime.now(timezone.utc) - timedelta(days=99)).isoformat()
            second = self._run(stored=first, ctx=apartment_ctx, enrich=enrich,
                               source=FakeSource(creatives=[_ad("a1")]))
            self.assertEqual([d["creativeId"] for d in second.dropped], ["a1"])

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
