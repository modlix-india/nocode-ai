"""search_autocomplete_locations - the map widget's typeahead backend.

Regression for PR #91 B4: a TokenServiceError (platform auth outage) was
swallowed into HTTP 200 [] - the UI rendered a broken search as "no matches"
with a green status (lived bug: 754 token failures looked like an empty
catalog). Auth outages must propagate so the route answers 503; ordinary
lookup noise still degrades to an empty candidate list.
"""
import asyncio
import unittest
from unittest.mock import patch

from app.agents.adzump.adapters.connections import TokenServiceError
from app.agents.adzump.adapters.google import client as google_client_mod
from app.agents.adzump.agents.location import search as search_mod
from app.agents.adzump.agents.location.search import search_autocomplete_locations


def _raises(exc: Exception):
    async def _call(*_a, **_kw):
        raise exc
    return _call


async def _geocode_none(_address):
    return None


class SearchAutocompleteTests(unittest.TestCase):
    def _search(self, platform, google_suggest=None, meta_get=None):
        with patch.object(google_client_mod.google_ads_client, "suggest_geo_targets",
                          side_effect=google_suggest or _raises(AssertionError("unused"))), \
                patch.object(search_mod.meta_client, "get",
                             side_effect=meta_get or _raises(AssertionError("unused"))), \
                patch.object(search_mod.google_maps_client, "geocode",
                             side_effect=_geocode_none):
            return asyncio.run(search_autocomplete_locations(
                q="Mumbai", platform=platform, client_code="C1",
                auth_headers={}, session_context={},
            ))

    def test_token_outage_propagates(self):
        outage = TokenServiceError("Token service failed")
        for platform, lookup_kwarg in (("google", "google_suggest"), ("meta", "meta_get")):
            with self.subTest(platform):
                with self.assertRaises(TokenServiceError):
                    self._search(platform, **{lookup_kwarg: _raises(outage)})

    def test_lookup_noise_degrades_to_empty(self):
        noise = RuntimeError("platform hiccup")
        for platform, lookup_kwarg in (("google", "google_suggest"), ("meta", "meta_get")):
            with self.subTest(platform):
                self.assertEqual(self._search(platform, **{lookup_kwarg: _raises(noise)}), [])

    def test_google_success_returns_candidates(self):
        async def _suggest(*_a, **_kw):
            return {"geoTargetConstantSuggestions": [{"geoTargetConstant": {
                "id": "1007785", "name": "Mumbai", "canonicalName": "Mumbai, MH, India",
                "targetType": "City",
            }}]}

        candidates = self._search("google", google_suggest=_suggest)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["name"], "Mumbai")
        self.assertEqual(candidates[0]["type"], "City")

    def test_short_query_returns_empty_without_lookups(self):
        candidates = asyncio.run(search_autocomplete_locations(
            q="M", platform="google", client_code="C1",
            auth_headers={}, session_context={},
        ))
        self.assertEqual(candidates, [])


if __name__ == "__main__":
    unittest.main()
