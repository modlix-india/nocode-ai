"""competitor_urls: the GBP guards (name normalize/match), the D-6
project-specific token test, the CP-4 ladder (GBP -> liveness -> listing-site
extraction -> D-7 keep-best), and the shared session memo."""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.competitor_urls import (
    _distinctive_tokens,
    _is_project_specific,
    cached_business_listing,
    listing_name_matches,
    normalize_business_name,
    resolve_project_url,
)


def _session(address: str = "") -> dict:
    return {"product_data": {"place": {"lat": 12.9, "lng": 77.6,
                                       "address": address}}}


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
                self.assertEqual(normalize_business_name(raw), expected)


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
                self.assertIs(listing_name_matches(candidate, listing), expected)


class ProjectSpecificTokenTests(unittest.TestCase):
    """D-6: a distinctive project-name token (leading brand token excluded -
    'sobha' proves nothing on sobha.com) prefix-matched in the HOST, or in the
    PATH of a brand-owned host - never on a third-party host."""

    def test_rows(self):
        rows = [
            ("microsite passes", "Purva Sparkling Springs",
             "https://purvasparklingspring.com/", True),
            ("bare developer root fails", "Sobha Magnus",
             "https://sobha.com/", False),
            ("developer category page fails", "Purva Sparkling Springs",
             "https://www.puravankara.com/villas-in-bannerghatta-road", False),
            ("project page on developer domain passes", "Sobha Magnus",
             "https://sobha.com/residential-projects/sobha-magnus", True),
            ("project slug on third-party host fails", "Sobha Magnus",
             "https://propsoch.com/sobha-magnus", False),
            ("generic-word-only name never specific", "Sobha Villas",
             "https://sobhavillas.com/", False),
        ]
        for label, name, url, expected in rows:
            with self.subTest(label):
                self.assertIs(
                    _is_project_specific(url, name, _session()), expected)

    def test_campaign_address_tokens_do_not_count(self):
        tokens = _distinctive_tokens(
            "Godrej Bannerghatta", _session("Bannerghatta Road, Bengaluru"))
        self.assertEqual(tokens, [])


class LadderTests(unittest.TestCase):
    """The CP-4 rungs, each mocked at its seam: GBP via GoogleMapsClient,
    liveness via _is_alive, extraction via fetch_and_answer."""

    def _resolve(self, name, current_url, *, listing, alive=True,
                 extraction_answer=None, session=None):
        session = session or _session()
        client = mock.Mock()
        client.find_business_website = mock.AsyncMock(return_value=listing)

        async def fake_fetch(url, question):
            if extraction_answer is None:
                raise ConnectionError("dead site")
            return {"status": "ok", "answer": extraction_answer,
                    "url": url, "title": "t"}

        with mock.patch(
            "app.agents.adzump.adapters.google.maps.GoogleMapsClient",
            return_value=client,
        ), mock.patch(
            "app.agents.adzump.competitor_urls._is_alive",
            new=mock.AsyncMock(return_value=alive),
        ), mock.patch(
            "app.agents.adzump.agents.product.adapters"
            ".web_fetch_adapter.fetch_and_answer",
            new=fake_fetch,
        ):
            result = asyncio.run(resolve_project_url(name, current_url, session))
        self.gbp_calls = client.find_business_website.await_count
        return result

    def test_project_specific_current_url_short_circuits(self):
        url = "https://purvasparklingspring.com/"
        result = self._resolve("Purva Sparkling Springs", url, listing=None)
        self.assertEqual(result, url)
        self.assertEqual(self.gbp_calls, 0)

    def test_rung2_live_gbp_project_site_accepted(self):
        result = self._resolve(
            "Purva Sparkling Springs",
            "https://www.puravankara.com/villas-in-bannerghatta-road",
            listing={"name": "Purva Sparkling Springs",
                     "website": "https://purvasparklingspring.com/"})
        self.assertEqual(result, "https://purvasparklingspring.com/")

    def test_rung3_bare_root_extraction_finds_project_page(self):
        result = self._resolve(
            "Sobha Magnus", "https://propsoch.com/sobha-magnus",
            listing={"name": "SOBHA Magnus", "website": "https://sobha.com/"},
            extraction_answer=(
                "OFFICIAL_URL: https://sobha.com/residential-projects/sobha-magnus"))
        self.assertEqual(result,
                         "https://sobha.com/residential-projects/sobha-magnus")

    def test_rung3_cross_host_answer_rejected_then_live_root_wins(self):
        # D-7(2): the live GBP root beats a category-page current_url.
        result = self._resolve(
            "Sobha Magnus", "https://propsoch.com/sobha-magnus",
            listing={"name": "SOBHA Magnus", "website": "https://sobha.com/"},
            extraction_answer="OFFICIAL_URL: https://someotherhost.com/magnus")
        self.assertEqual(result, "https://sobha.com/")

    def test_extraction_none_and_dead_root_keep_current_url(self):
        # D-7(3): nothing live found - the entry keeps what search gave it.
        result = self._resolve(
            "Sobha Magnus", "https://propsoch.com/sobha-magnus",
            listing={"name": "SOBHA Magnus", "website": "https://sobha.com/"},
            alive=False, extraction_answer="OFFICIAL_URL: none")
        self.assertEqual(result, "https://propsoch.com/sobha-magnus")

    def test_guard_misses_keep_current_url(self):
        rows = [
            ("no listing", None),
            ("name mismatch", {"name": "Sobha Neopolis",
                               "website": "https://sobhaneopolis.com/"}),
            ("aggregator listing host", {"name": "Sobha Magnus",
                                         "website": "https://facebook.com/sm"}),
        ]
        for label, listing in rows:
            with self.subTest(label):
                result = self._resolve(
                    "Sobha Magnus", "https://propsoch.com/sobha-magnus",
                    listing=listing)
                self.assertEqual(result, "https://propsoch.com/sobha-magnus")

    def test_no_url_and_no_listing_stays_linkless(self):
        self.assertIsNone(self._resolve("Sobha Magnus", None, listing=None))

    def test_aggregator_current_url_never_settles(self):
        # The single-lookup add path has no _clean_urls; the ladder must scrub
        # an aggregator URL itself or it poisons the shared library key.
        result = self._resolve(
            "Lodha Azur", "https://lodha-azur.99acres.com/", listing=None)
        self.assertIsNone(result)


class ListingMemoTests(unittest.TestCase):
    def test_one_lookup_per_name_and_misses_memoized(self):
        session = _session()
        client = mock.Mock()
        client.find_business_website = mock.AsyncMock(return_value=None)
        with mock.patch(
            "app.agents.adzump.adapters.google.maps.GoogleMapsClient",
            return_value=client,
        ):
            for _ in range(2):  # miss memoized - no retry storm
                result = asyncio.run(
                    cached_business_listing("Sobha Magnus", session))
        self.assertIsNone(result)
        self.assertEqual(client.find_business_website.await_count, 1)
        self.assertIn("sobha magnus", session["_places_website_cache"])


if __name__ == "__main__":
    unittest.main()
