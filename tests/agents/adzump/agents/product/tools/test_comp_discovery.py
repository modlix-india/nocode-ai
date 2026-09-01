"""comp_discovery helpers: _normalize_name (brand dedup), _is_specific_geography
(geo hard-floor), _listing_name_matches + _resolve_urls (Places-first, D-5) +
the dead-GBP fetch fallback."""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.agents.product.tools.comp_discovery import (
    _dedupe_resolved_hosts,
    _fetch_one_for_shortlist,
    _listing_name_matches,
    _normalize_name,
    _is_specific_geography,
    _resolve_urls,
)


class NormalizeNameLock(unittest.TestCase):

    def test_brand_canonicalisation(self):
        cases = [
            ("Valmark CityVille", "valmark cityville"),
            ("Sumadhura Group", "sumadhura"),            # " group" suffix stripped
            ("Puravankara Pvt Ltd", "puravankara"),      # " pvt ltd" stripped
            ("Sattva-Songbird", "sattva songbird"),      # punctuation → space
            ("  Sobha  ", "sobha"),                      # strip + collapse
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(_normalize_name(raw), expected)


class IsSpecificGeographyLock(unittest.TestCase):

    def test_specific_localities_true(self):
        # Marker word (road/layout/block) OR compound suffix (-nagar).
        for geo in ["Sarjapur Road", "HSR Layout", "Indiranagar",
                    "Koramangala 5th Block", "Whitefield Main Road"]:
            with self.subTest(geo=geo):
                self.assertTrue(_is_specific_geography(geo))

    def test_city_regional_or_empty_false(self):
        for geo in ["Bengaluru", "Karnataka", "India", "", None]:
            with self.subTest(geo=geo):
                self.assertFalse(_is_specific_geography(geo))


class ListingNameMatchTests(unittest.TestCase):
    def test_match_rows(self):
        rows = [
            ("exact", "Lodha Azur", "Lodha Azur", True),
            ("listing has suffix", "Lodha Azur", "Lodha Azur by Lodha Group", True),
            ("spacing/case noise", "Purva Sparkling Springs",
             "PURVA Sparkling-Springs", True),
            ("different project, same brand", "Lodha Azur", "Lodha Bellezza", False),
            ("unrelated", "Sobha Galera", "Prestige Falcon City", False),
            ("empty listing", "Lodha Azur", "", False),
        ]
        for label, candidate, listing, expected in rows:
            with self.subTest(label):
                self.assertIs(_listing_name_matches(candidate, listing), expected)


class ResolveUrlsTests(unittest.TestCase):
    """Places-first resolution (D-5): every candidate gets one GBP lookup and a
    guard-passing listing WINS over the search URL; a wrong or shared-host
    listing never lands (a bad URL would poison the shared creative-library
    key); a guard miss keeps the search URL - the floor is the old behavior."""

    SESSION = {"product_data": {"place": {"lat": 12.9, "lng": 77.6}}}

    def _run(self, candidates, listing):
        client = mock.Mock()
        listings = listing if isinstance(listing, list) else [listing]
        client.find_business_website = mock.AsyncMock(side_effect=listings)
        with mock.patch(
            "app.agents.adzump.adapters.google.maps.GoogleMapsClient",
            return_value=client,
        ):
            asyncio.run(_resolve_urls(candidates, self.SESSION))
        return client

    def test_gbp_wins_over_search_url_keeping_it_as_fallback(self):
        candidates = [
            {"name": "Purva Sparkling Springs",
             "url": "https://www.puravankara.com/villas-in-bannerghatta-road"},
            {"name": "Lodha Azur", "url": None},
        ]
        client = self._run(candidates, [
            {"name": "Purva Sparkling Springs",
             "website": "https://purvasparklingspring.com/"},
            {"name": "Lodha Azur", "website": "https://lodhagroup.com/azur"},
        ])
        self.assertEqual(candidates[0]["url"], "https://purvasparklingspring.com/")
        self.assertEqual(candidates[0]["search_url"],
                         "https://www.puravankara.com/villas-in-bannerghatta-road")
        self.assertEqual(candidates[1]["url"], "https://lodhagroup.com/azur")
        self.assertNotIn("search_url", candidates[1])  # nothing displaced
        self.assertEqual(client.find_business_website.await_count, 2)
        kwargs = client.find_business_website.await_args.kwargs
        self.assertEqual((kwargs["lat"], kwargs["lng"]), (12.9, 77.6))

    def test_same_host_listing_swaps_nothing(self):
        candidates = [{"name": "Sobha", "url": "https://sobha.com/galera"}]
        self._run(candidates, {"name": "Sobha", "website": "https://www.sobha.com"})
        self.assertEqual(candidates[0]["url"], "https://sobha.com/galera")
        self.assertNotIn("search_url", candidates[0])

    def test_guard_misses_keep_the_search_url(self):
        rows = [
            ("name mismatch", {"name": "Lodha Bellezza",
                               "website": "https://lodhagroup.com/bellezza"}),
            ("shared host", {"name": "Lodha Azur",
                             "website": "https://facebook.com/lodhaazur"}),
            ("no listing", None),
        ]
        for label, listing in rows:
            with self.subTest(label):
                candidates = [{"name": "Lodha Azur", "url": "https://99acres.com/x"}]
                self._run(candidates, listing)
                self.assertEqual(candidates[0]["url"], "https://99acres.com/x")


class DedupeResolvedHostsTests(unittest.TestCase):
    def test_keeps_first_per_host_and_urlless_pass_through(self):
        candidates = [
            {"name": "Purva A", "url": "https://puravankara.com/a"},
            {"name": "Purva B", "url": "https://www.puravankara.com/b"},
            {"name": "No Site", "url": None},
        ]
        kept = _dedupe_resolved_hosts(candidates)
        self.assertEqual([c["name"] for c in kept], ["Purva A", "No Site"])


class FetchFallbackTests(unittest.TestCase):
    """A dead GBP website retries once with the displaced search URL (D-5)."""

    def _fetch(self, candidate, answers_by_url):
        async def fake_fetch(url, question):
            answer = answers_by_url.get(url)
            if answer is None:
                raise ConnectionError("dead site")
            return {"status": "ok", "answer": answer, "url": url, "title": "t"}
        with mock.patch(
            "app.agents.adzump.agents.product.adapters.web_fetch_adapter.fetch_and_answer",
            new=fake_fetch,
        ):
            return asyncio.run(_fetch_one_for_shortlist(candidate))

    def test_dead_gbp_url_falls_back_to_search_url(self):
        result = self._fetch(
            {"name": "Purva", "url": "https://deadmicrosite.com",
             "search_url": "https://puravankara.com/villas"},
            {"https://puravankara.com/villas": "TYPE: BRAND\nGood page"},
        )
        self.assertEqual(result["fetch_status"], "ok")
        self.assertEqual(result["url"], "https://puravankara.com/villas")

    def test_both_dead_fails(self):
        result = self._fetch(
            {"name": "Purva", "url": "https://deadmicrosite.com",
             "search_url": "https://alsodead.com"},
            {},
        )
        self.assertEqual(result["fetch_status"], "failed")


if __name__ == "__main__":
    unittest.main()
