"""PlatformGeoMapper - every mapped location carries a composed platform handle.

Regression for the live finding (meta-location-type-missing): Meta adset creation
buckets each target by type (zips/cities/regions/…), so the resolved handle must
always carry a type. The mapper now returns a composed TargetArea: generic 'where'
at the top level, the platform handle nested under `meta` / `google`.

Below the model - meta_client.get / google_ads_client.suggest_geo_targets /
google_maps_client.geocode are mocked.
"""
import asyncio
import unittest
from unittest.mock import patch

from app.agents.adzump.agents.location import platform_mapping as mapping_mod
from app.agents.adzump.agents.location.platform_mapping import PlatformGeoMapper


def _meta_get(data: list):
    """Async stub for meta_client.get returning a fixed /search payload."""
    async def _get(*_a, **_kw):
        return {"data": data}
    return _get


def _meta_raises():
    async def _get(*_a, **_kw):
        raise RuntimeError("Meta /search down")
    return _get


async def _no_geocode(_query):
    return None


class MapMetaTypeTests(unittest.TestCase):
    def _map(self, area, meta_get):
        mapper = PlatformGeoMapper({})
        with patch.object(mapping_mod.meta_client, "get", side_effect=meta_get), \
                patch.object(mapping_mod.google_maps_client, "geocode", side_effect=_no_geocode):
            return asyncio.run(mapper._map_meta(dict(area), "IN"))

    def test_a_match_sets_the_type(self):
        # Meta buckets each target by type, so the handle always carries one.
        for label, area, found, expected in [
            ("a city match", {"name": "Bandra", "city": "Bandra"},
             [{"key": "1234", "name": "Bandra", "type": "city"}],
             {"type": "city", "key": "1234", "name": "Bandra"}),
            # searched as a city, but Meta's own type wins
            ("Meta's canonical type wins", {"name": "Goa", "city": "Goa"},
             [{"key": "777", "name": "Goa", "type": "region"}],
             {"type": "region", "key": "777", "name": "Goa"}),
            ("a name-only area searches as a city", {"name": "Some Neighborhood"},
             [{"key": "999", "name": "Some Neighborhood"}],
             {"type": "city", "key": "999", "name": "Some Neighborhood"}),
        ]:
            with self.subTest(label):
                self.assertEqual(self._map(area, _meta_get(found))["meta"], expected)

    def test_no_match_no_handle(self):
        # keyless Meta entries are invalid; the type lives only under meta.*
        pincode = {"name": "400050", "pincode": "400050"}
        for label, meta_get in [("no match", _meta_get([])), ("lookup failed", _meta_raises())]:
            with self.subTest(label):
                out = self._map(pincode, meta_get)
                for key in ("meta", "type", "scale"):
                    self.assertNotIn(key, out)

    def test_broad_scales_search_as_their_type(self):
        # a national campaign's scale="country" must not be searched as a city
        for label, area, found, searched in [
            ("country", {"name": "India", "scale": "country"},
             {"key": "IN", "name": "India", "type": "country"}, '"country"'),
            ("state searches as a region", {"name": "Karnataka", "scale": "state"},
             {"key": "456", "name": "Karnataka", "type": "region"}, '"region"'),
        ]:
            with self.subTest(label):
                captured = {}

                async def _get(*_a, found=found, **kw):
                    captured["params"] = kw.get("params") or {}
                    return {"data": [found]}

                self.assertEqual(self._map(area, _get)["meta"], found)
                self.assertIn(searched, captured["params"]["location_types"])

    def test_existing_nested_handle_preserved(self):
        # A prior-mapping round-trip arrives nested → kept without a re-lookup.
        def _should_not_call(*_a, **_kw):
            raise AssertionError("meta_client.get must not be called when key exists")

        out = self._map(
            {"name": "Bandra", "city": "Bandra",
             "meta": {"type": "city", "key": "999", "name": "Bandra"}},
            _should_not_call,
        )
        self.assertEqual(out["meta"], {"type": "city", "key": "999", "name": "Bandra"})

    def test_flat_key_is_untrusted_and_relooked_up(self):
        # PR #91 B2: a flat key is LLM-writable, so it must NOT skip the
        # lookup - the mapper re-derives the handle from Meta's own /search.
        out = self._map(
            {"name": "400050", "pincode": "400050", "key": "hallucinated"},
            _meta_get([{"key": "1234", "name": "400050", "type": "zip"}]),
        )
        self.assertEqual(out["meta"]["key"], "1234")   # lookup wins, flat ignored
        self.assertNotIn("key", out)                    # flat field not persisted


class BackfillPincodeTests(unittest.TestCase):
    """Neighbourhood-scale areas get their postal code reverse-geocoded so the
    craft map can draw a real Feature Layer polygon; broad areas must not."""

    _POSTAL_RESULT = [{
        "types": ["postal_code"],
        "address_components": [
            {"types": ["postal_code"], "long_name": "560038"},
        ],
    }]

    def _backfill(self, area, reverse_results):
        calls = []

        async def _reverse(lat, lng):
            calls.append((lat, lng))
            if isinstance(reverse_results, Exception):
                raise reverse_results
            return reverse_results

        mapper = PlatformGeoMapper({})
        with patch.object(mapping_mod.google_maps_client, "reverse_geocode",
                          side_effect=_reverse):
            asyncio.run(mapper._backfill_pincode(area))
        return area, calls

    def test_backfill_variants(self):
        variants = [
            ("neighbourhood gets pincode",
             {"name": "Indiranagar", "lat": 12.97, "lng": 77.64},
             self._POSTAL_RESULT, "560038", 1),
            ("broad scale skipped",
             {"name": "Mumbai", "scale": "city", "lat": 19.07, "lng": 72.87},
             self._POSTAL_RESULT, None, 0),
            ("region scale skipped",  # PR #91 B6: was missing from BROAD_SCALES
             {"name": "South India", "scale": "region", "lat": 12.0, "lng": 78.0},
             self._POSTAL_RESULT, None, 0),
            ("existing pincode kept",
             {"name": "HSR", "pincode": "560102", "lat": 12.9, "lng": 77.6},
             self._POSTAL_RESULT, "560102", 0),
            ("no coordinates skipped",
             {"name": "Juhu"},
             self._POSTAL_RESULT, None, 0),
            ("no postal candidate leaves area unchanged",
             {"name": "Nowhere", "lat": 1.0, "lng": 1.0},
             [{"types": ["locality"], "address_components": []}], None, 1),
            ("reverse-geocode failure survives",
             {"name": "Flaky", "lat": 2.0, "lng": 2.0},
             RuntimeError("Maps down"), None, 1),
        ]
        for label, area, reverse_results, expected_pincode, expected_calls in variants:
            with self.subTest(label):
                out, calls = self._backfill(dict(area), reverse_results)
                self.assertEqual(out.get("pincode"), expected_pincode)
                self.assertEqual(len(calls), expected_calls)


class MapTargetAreasTests(unittest.TestCase):
    """PR #91 J2: enrichment (geocode + pincode backfill) is hoisted out of
    the mappers and runs BEFORE the platform lookup; areas map concurrently."""

    def test_enriches_then_maps(self):
        captured = {}

        async def _geocode(_query):
            return {"lat": 12.97, "lng": 77.64}

        async def _reverse(_lat, _lng):
            return [{"types": ["postal_code"],
                     "address_components": [{"types": ["postal_code"],
                                             "long_name": "560038"}]}]

        async def _get(*_a, **kw):
            captured["params"] = kw.get("params") or {}
            return {"data": [{"key": "z1", "name": "560038", "type": "zip"}]}

        mapper = PlatformGeoMapper({})
        with patch.object(mapping_mod.google_maps_client, "geocode", side_effect=_geocode), \
                patch.object(mapping_mod.google_maps_client, "reverse_geocode", side_effect=_reverse), \
                patch.object(mapping_mod.meta_client, "get", side_effect=_get):
            out = asyncio.run(mapper.map_target_areas(
                [{"name": "Indiranagar"}], "Meta", "IN"))

        self.assertEqual(out[0]["pincode"], "560038")        # backfilled pre-lookup
        self.assertEqual(captured["params"]["q"], "560038")  # lookup used the pincode
        self.assertEqual(out[0]["meta"]["type"], "zip")


class MapGoogleTests(unittest.TestCase):
    def _map(self, area, suggestions):
        async def _suggest(*_a, **_kw):
            return {"geoTargetConstantSuggestions": suggestions}

        mapper = PlatformGeoMapper({})
        with patch.object(mapping_mod.google_ads_client, "suggest_geo_targets", side_effect=_suggest), \
                patch.object(mapping_mod.google_maps_client, "geocode", side_effect=_no_geocode):
            return asyncio.run(mapper._map_google(dict(area), "IN"))

    def test_rows(self):
        for label, suggestions, expected in [
            ("a match gets a normalized resource name",
             [{"geoTargetConstant": {"id": "1007785", "canonicalName": "Bengaluru"}}],
             {"resourceName": "geoTargetConstants/1007785", "name": "Bengaluru"}),
            # keyless: proximity fallback, no handle
            ("no match: no handle", [], None),
        ]:
            with self.subTest(label):
                out = self._map({"name": "Bengaluru", "city": "Bengaluru"}, suggestions)
                self.assertEqual(out.get("google"), expected)


if __name__ == "__main__":
    unittest.main()
