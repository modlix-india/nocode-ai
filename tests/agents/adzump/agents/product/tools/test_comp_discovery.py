"""comp_discovery helpers: _normalize_name (brand dedup), _is_specific_geography
(geo hard-floor), _listing_name_matches + _resolve_missing_urls (Places URL rung)."""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.agents.product.tools.comp_discovery import (
    _listing_name_matches,
    _normalize_name,
    _is_specific_geography,
    _resolve_missing_urls,
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


class ResolveMissingUrlsTests(unittest.TestCase):
    """The Places rung: fills only missing/aggregator URLs, and a wrong or
    shared-host listing never lands (a bad URL would poison the shared
    creative-library key)."""

    SESSION = {"product_data": {"place": {"lat": 12.9, "lng": 77.6}}}

    def _run(self, candidates, listing):
        client = mock.Mock()
        listings = listing if isinstance(listing, list) else [listing]
        client.find_business_website = mock.AsyncMock(side_effect=listings)
        with mock.patch(
            "app.agents.adzump.adapters.google.maps.GoogleMapsClient",
            return_value=client,
        ):
            asyncio.run(_resolve_missing_urls(candidates, self.SESSION))
        return client

    def test_fills_urlless_and_aggregator_candidates_only(self):
        candidates = [
            {"name": "Lodha Azur", "url": None},
            {"name": "Purva", "url": "https://www.99acres.com/purva-listing"},
            {"name": "Sobha", "url": "https://sobha.com"},
        ]
        client = self._run(candidates, [
            {"name": "Lodha Azur", "website": "https://lodhagroup.com/azur"},
            {"name": "Purva", "website": "https://purvasparkling.com"},
        ])
        self.assertEqual(candidates[0]["url"], "https://lodhagroup.com/azur")
        self.assertEqual(candidates[1]["url"], "https://purvasparkling.com")
        self.assertEqual(candidates[2]["url"], "https://sobha.com")  # untouched
        self.assertEqual(client.find_business_website.await_count, 2)
        kwargs = client.find_business_website.await_args.kwargs
        self.assertEqual((kwargs["lat"], kwargs["lng"]), (12.9, 77.6))

    def test_rejects_name_mismatch(self):
        candidates = [{"name": "Lodha Azur", "url": None}]
        self._run(candidates, {"name": "Lodha Bellezza",
                               "website": "https://lodhagroup.com/bellezza"})
        self.assertIsNone(candidates[0]["url"])

    def test_rejects_shared_host_website(self):
        candidates = [{"name": "Lodha Azur", "url": None}]
        self._run(candidates, {"name": "Lodha Azur",
                               "website": "https://facebook.com/lodhaazur"})
        self.assertIsNone(candidates[0]["url"])

    def test_no_lookup_when_all_resolved(self):
        candidates = [{"name": "Sobha", "url": "https://sobha.com"}]
        client = self._run(candidates, None)
        self.assertEqual(client.find_business_website.await_count, 0)

    def test_no_listing_leaves_candidate_untouched(self):
        candidates = [{"name": "Lodha Azur", "url": None}]
        self._run(candidates, None)
        self.assertIsNone(candidates[0]["url"])


if __name__ == "__main__":
    unittest.main()
