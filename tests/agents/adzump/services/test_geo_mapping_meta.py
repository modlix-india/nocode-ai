"""PlatformGeoMapper — every mapped location carries a composed platform handle.

Regression for the live finding (meta-location-type-missing): Meta adset creation
buckets each target by type (zips/cities/regions/…), so the resolved handle must
always carry a type. The mapper now returns a composed TargetArea: generic 'where'
at the top level, the platform handle nested under `meta` / `google`.

Below the model — meta_client.get / google_ads_client.suggest_geo_targets /
google_maps_client.geocode are mocked.
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

    def test_type_set_when_lookup_succeeds(self):
        out = self._map(
            {"name": "Bandra", "city": "Bandra"},
            _meta_get([{"key": "1234", "name": "Bandra", "type": "city"}]),
        )
        self.assertEqual(out["meta"], {"type": "city", "key": "1234", "name": "Bandra"})

    def test_prefers_meta_canonical_type_over_assumed(self):
        # We searched with location_types=["city"] but Meta classified it a region —
        # trust Meta's own type, not the field-derived loc_type.
        out = self._map(
            {"name": "Goa", "city": "Goa"},
            _meta_get([{"key": "777", "name": "Goa", "type": "region"}]),
        )
        self.assertEqual(out["meta"]["type"], "region")

    def test_type_present_when_lookup_returns_empty(self):
        # No match → typed handle with no key (downstream radial fallback).
        out = self._map({"name": "400050", "pincode": "400050"}, _meta_get([]))
        self.assertEqual(out["meta"]["type"], "zip")
        self.assertNotIn("key", out["meta"])

    def test_type_present_when_lookup_raises(self):
        out = self._map({"name": "400050", "pincode": "400050"}, _meta_raises())
        self.assertEqual(out["meta"]["type"], "zip")
        self.assertNotIn("key", out["meta"])

    def test_name_only_area_defaults_to_city_type(self):
        out = self._map({"name": "Some Neighborhood"}, _meta_get([]))
        self.assertEqual(out["meta"]["type"], "city")

    def test_no_duplicated_flat_type_in_output(self):
        out = self._map({"name": "400050", "pincode": "400050"}, _meta_get([]))
        self.assertNotIn("meta_type", out)   # type lives only under meta.*
        self.assertNotIn("scale", out)        # local area carried no scale

    def test_country_level_searches_as_country(self):
        # National/international campaigns tag scale="country"; it must be searched
        # (and typed) as a country, not mis-searched as a city.
        captured = {}

        async def _get(*_a, **kw):
            captured["params"] = kw.get("params") or {}
            return {"data": [{"key": "IN", "name": "India", "type": "country"}]}

        out = self._map({"name": "India", "scale": "country"}, _get)
        self.assertEqual(out["meta"], {"type": "country", "key": "IN", "name": "India"})
        self.assertIn('"country"', captured["params"]["location_types"])

    def test_state_level_searches_as_region(self):
        captured = {}

        async def _get(*_a, **kw):
            captured["params"] = kw.get("params") or {}
            return {"data": [{"key": "456", "name": "Karnataka", "type": "region"}]}

        out = self._map({"name": "Karnataka", "scale": "state"}, _get)
        self.assertEqual(out["meta"]["type"], "region")
        self.assertIn('"region"', captured["params"]["location_types"])

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

    def test_flat_widget_key_preserved_and_nested(self):
        # The search widget supplies a flat meta_key → consumed, no re-lookup,
        # emitted in nested form.
        def _should_not_call(*_a, **_kw):
            raise AssertionError("meta_client.get must not be called when key exists")

        out = self._map(
            {"name": "400050", "pincode": "400050", "meta_key": "555"},
            _should_not_call,
        )
        self.assertEqual(out["meta"]["key"], "555")
        self.assertEqual(out["meta"]["type"], "zip")


class MapGoogleTests(unittest.TestCase):
    def _map(self, area, suggestions):
        async def _suggest(*_a, **_kw):
            return {"geoTargetConstantSuggestions": suggestions}

        mapper = PlatformGeoMapper({}, {})
        with patch.object(mapping_mod.google_ads_client, "suggest_geo_targets", side_effect=_suggest), \
                patch.object(mapping_mod.google_maps_client, "geocode", side_effect=_no_geocode):
            return asyncio.run(mapper._map_google(dict(area), "IN"))

    def test_resource_name_normalized(self):
        out = self._map(
            {"name": "Bengaluru", "city": "Bengaluru"},
            [{"geoTargetConstant": {"id": "1007785", "canonicalName": "Bengaluru"}}],
        )
        self.assertEqual(
            out["google"],
            {"resourceName": "geoTargetConstants/1007785", "name": "Bengaluru"},
        )

    def test_no_match_leaves_no_google_handle(self):
        out = self._map({"name": "Nowhere", "city": "Nowhere"}, [])
        self.assertNotIn("google", out)       # keyless → proximity fallback, no handle


if __name__ == "__main__":
    unittest.main()
