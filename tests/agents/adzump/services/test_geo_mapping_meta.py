"""PlatformGeoMapper._map_meta — every Meta-mapped location must carry meta_type.

Regression for the live finding (meta-location-type-missing): Meta adset creation
buckets each target by type (zips/cities/regions/…), so `meta_type` is required on
every mapped location. The old code only stamped meta_type when the /search lookup
returned a key — so a failed/empty lookup (or a widget-supplied key that skipped the
lookup) left the location with no type, and adset creation broke.

Below the model — meta_client.get and google_maps_client.geocode are mocked.
"""
import asyncio
import unittest
from unittest.mock import patch

from app.agents.adzump.services.geo import mapping as mapping_mod
from app.agents.adzump.services.geo.mapping import PlatformGeoMapper


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
        mapper = PlatformGeoMapper({}, {})
        with patch.object(mapping_mod.meta_client, "get", side_effect=meta_get), \
                patch.object(mapping_mod.google_maps_client, "geocode", side_effect=_no_geocode):
            return asyncio.run(mapper._map_meta(dict(area), "IN"))

    def test_type_stamped_when_lookup_succeeds(self):
        out = self._map(
            {"name": "Bandra", "city": "Bandra"},
            _meta_get([{"key": "1234", "name": "Bandra", "type": "city"}]),
        )
        self.assertEqual(out["meta_key"], "1234")
        self.assertEqual(out["meta_type"], "city")

    def test_prefers_meta_canonical_type_over_assumed(self):
        # We searched with location_types=["city"] but Meta classified it a region —
        # trust Meta's own type, not the field-derived loc_type.
        out = self._map(
            {"name": "Goa", "city": "Goa"},
            _meta_get([{"key": "777", "name": "Goa", "type": "region"}]),
        )
        self.assertEqual(out["meta_type"], "region")

    def test_type_present_when_lookup_returns_empty(self):
        # No match → no key, but the type must still be stamped from the field-derived
        # loc_type so downstream adset creation has it.
        out = self._map({"name": "400050", "pincode": "400050"}, _meta_get([]))
        self.assertNotIn("meta_key", out)
        self.assertEqual(out["meta_type"], "zip")

    def test_type_present_when_lookup_raises(self):
        out = self._map({"name": "400050", "pincode": "400050"}, _meta_raises())
        self.assertNotIn("meta_key", out)
        self.assertEqual(out["meta_type"], "zip")

    def test_name_only_area_defaults_to_city_type(self):
        out = self._map({"name": "Some Neighborhood"}, _meta_get([]))
        self.assertEqual(out["meta_type"], "city")

    def test_existing_meta_key_preserved_and_typed(self):
        # Search widget already resolved the key+type → no re-lookup, values kept.
        def _should_not_call(*_a, **_kw):
            raise AssertionError("meta_client.get must not be called when key exists")

        out = self._map(
            {"name": "Bandra", "city": "Bandra", "meta_key": "999", "meta_type": "city"},
            _should_not_call,
        )
        self.assertEqual(out["meta_key"], "999")
        self.assertEqual(out["meta_type"], "city")

    def test_country_level_searches_as_country(self):
        # National/international campaigns tag geo_level="country"; it must be
        # searched (and typed) as a country, not mis-searched as a city.
        captured = {}

        async def _get(*_a, **kw):
            captured["params"] = kw.get("params") or {}
            return {"data": [{"key": "IN", "name": "India", "type": "country"}]}

        out = self._map({"name": "India", "geo_level": "country"}, _get)
        self.assertEqual(out["meta_type"], "country")
        self.assertEqual(out["meta_key"], "IN")
        self.assertIn('"country"', captured["params"]["location_types"])

    def test_state_level_searches_as_region(self):
        captured = {}

        async def _get(*_a, **kw):
            captured["params"] = kw.get("params") or {}
            return {"data": [{"key": "456", "name": "Karnataka", "type": "region"}]}

        out = self._map({"name": "Karnataka", "geo_level": "state"}, _get)
        self.assertEqual(out["meta_type"], "region")
        self.assertIn('"region"', captured["params"]["location_types"])

    def test_existing_key_without_type_gets_type_stamped(self):
        # Widget supplied a key but no type → skip lookup, still stamp from loc_type.
        def _should_not_call(*_a, **_kw):
            raise AssertionError("meta_client.get must not be called when key exists")

        out = self._map(
            {"name": "400050", "pincode": "400050", "meta_key": "555"},
            _should_not_call,
        )
        self.assertEqual(out["meta_key"], "555")
        self.assertEqual(out["meta_type"], "zip")


class MapGoogleTests(unittest.TestCase):
    def _map(self, area, suggestions):
        async def _suggest(*_a, **_kw):
            return {"geoTargetConstantSuggestions": suggestions}

        mapper = PlatformGeoMapper({}, {})
        with patch.object(mapping_mod.google_ads_client, "suggest_geo_targets", side_effect=_suggest), \
                patch.object(mapping_mod.google_maps_client, "geocode", side_effect=_no_geocode):
            return asyncio.run(mapper._map_google(dict(area), "IN"))

    def test_google_id_normalized_to_resource_name(self):
        out = self._map(
            {"name": "Bengaluru", "city": "Bengaluru"},
            [{"geoTargetConstant": {"id": "1007785", "canonicalName": "Bengaluru"}}],
        )
        self.assertEqual(out["google_id"], "geoTargetConstants/1007785")
        self.assertEqual(out["google_name"], "Bengaluru")

    def test_no_match_leaves_no_google_id(self):
        out = self._map({"name": "Nowhere", "city": "Nowhere"}, [])
        self.assertNotIn("google_id", out)


if __name__ == "__main__":
    unittest.main()
