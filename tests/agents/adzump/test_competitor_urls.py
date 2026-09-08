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
    is_broker_style_tld,
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
            # Parenthetical glosses die (CP-5 v2 step 13): developer credits
            # and duplicated brand tokens must not leak into matching/tokens.
            ("Purva Sparkling Springs (Puravankara)", "purva sparkling springs"),
            ("Nambiar Villas (Nambiar Bannerghatta Villas)", "nambiar villas"),
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
            # Live 2026-09-04: the duplicated brand token inside "(...)" must
            # not make a clone domain "project-specific" and skip the ladder.
            ("parenthetical brand dup never short-circuits",
             "Nambiar Villas (Nambiar Bannerghatta Villas)",
             "https://nambiarbannerghattaroad.co.in/", False),
        ]
        for label, name, url, expected in rows:
            with self.subTest(label):
                self.assertIs(
                    _is_project_specific(url, name, _session()), expected)

    def test_campaign_address_tokens_do_not_count(self):
        tokens = _distinctive_tokens(
            "Godrej Bannerghatta", _session("Bannerghatta Road, Bengaluru"))
        self.assertEqual(tokens, [])


class BrokerStyleTldTests(unittest.TestCase):
    """Kailash's prior: not-plain-.com/.in hosts are broker clones ~90% of the
    time in Indian real estate - never acceptable as an entry's official URL."""

    def test_rows(self):
        rows = [
            ("nambiarvillasbannerghatta.co.in", True),   # .co.in swarm
            ("nambiarbannerghatta.info", True),
            ("prestigesouthernstar.live", True),
            ("myanimelist.net:443", True),               # port stripped first
            ("purvasparklingspring.com", False),
            ("valmark.in", False),
            ("", False),
        ]
        for host, expected in rows:
            with self.subTest(host):
                self.assertIs(is_broker_style_tld(host), expected)


class LadderTests(unittest.TestCase):
    """The CP-4 rungs, each mocked at its seam: GBP via GoogleMapsClient,
    liveness via is_alive, extraction via fetch_and_answer."""

    def _resolve(self, name, current_url, *, listing, alive=True,
                 extraction_answer=None, session=None):
        session = session or _session()
        client = mock.Mock()
        client.find_business_listings = mock.AsyncMock(
            return_value=[listing] if listing else [])

        async def fake_fetch(url, question):
            if extraction_answer is None:
                raise ConnectionError("dead site")
            return {"status": "ok", "answer": extraction_answer,
                    "url": url, "title": "t"}

        with mock.patch(
            "app.agents.adzump.adapters.google.maps.GoogleMapsClient",
            return_value=client,
        ), mock.patch(
            "app.agents.adzump.competitor_urls.is_alive",
            new=mock.AsyncMock(return_value=alive),
        ), mock.patch(
            "app.agents.adzump.agents.product.adapters"
            ".web_fetch_adapter.fetch_and_answer",
            new=fake_fetch,
        ):
            result = asyncio.run(resolve_project_url(name, current_url, session))
        self.gbp_calls = client.find_business_listings.await_count
        return result

    def test_project_specific_current_url_short_circuits(self):
        url = "https://purvasparklingspring.com/"
        result = self._resolve("Purva Sparkling Springs", url, listing=None)
        self.assertEqual(result, url)
        self.assertEqual(self.gbp_calls, 0)

    def test_dead_project_specific_url_never_short_circuits(self):
        # D-9 + live 2026-09-08: a DEAD lookalike domain (rainbowmayfaire.com)
        # short-circuited the ladder forever; the correct GBP site (rung 2)
        # was never consulted. Dead current_url must ride the full ladder.
        result = self._resolve(
            "Rainbow Mayfair", "https://rainbowmayfaire.com/",
            listing={"name": "Rainbow Mayfair",
                     "website": "https://rainbowmayfair.com/"},
            alive=False)
        # Everything is dead in this fixture, so the search URL is kept -
        # the point is the ladder RAN (GBP consulted, no short-circuit).
        self.assertEqual(self.gbp_calls, 1)

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

    def test_broker_style_urls_never_become_official(self):
        with self.subTest("broker current_url scrubbed to link-less"):
            result = self._resolve(
                "Nambiar Villas", "https://nambiarbannerghatta.info/",
                listing=None)
            self.assertIsNone(result)
        with self.subTest("broker-domain GBP listing rejected, search URL kept"):
            # Brokers claim GBP listings; a live .co.in on the listing is a
            # claimed profile, not the project's identity.
            result = self._resolve(
                "Nambiar Villas", "https://propsoch.com/nambiar",
                listing={"name": "Nambiar Villas Bannerghatta",
                         "website": "https://nambiarvillasbannerghatta.co.in/"})
            self.assertEqual(result, "https://propsoch.com/nambiar")


class ListingMemoTests(unittest.TestCase):
    def test_one_lookup_per_name_and_misses_memoized(self):
        session = _session()
        client = mock.Mock()
        client.find_business_listings = mock.AsyncMock(return_value=[])
        with mock.patch(
            "app.agents.adzump.adapters.google.maps.GoogleMapsClient",
            return_value=client,
        ):
            for _ in range(2):  # miss memoized - no retry storm
                result = asyncio.run(
                    cached_business_listing("Sobha Magnus", session))
        self.assertIsNone(result)
        self.assertEqual(client.find_business_listings.await_count, 1)
        self.assertIn("sobha magnus", session["_places_listings_cache"])

    def test_top_listing_without_website_is_none_for_the_ladder(self):
        # A lower listing's website must not masquerade as the top match.
        session = _session()
        client = mock.Mock()
        client.find_business_listings = mock.AsyncMock(return_value=[
            {"name": "Sobha Magnus", "website": ""},
            {"name": "Sobha Magnus Broker Deals",
             "website": "https://sobhamagnusdeals.com/"},
        ])
        with mock.patch(
            "app.agents.adzump.adapters.google.maps.GoogleMapsClient",
            return_value=client,
        ):
            result = asyncio.run(
                cached_business_listing("Sobha Magnus", session))
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
