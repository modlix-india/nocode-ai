"""Geo-targeting location models — the typed contract for platform locations.

MetaGeoLocation makes the original bug structurally impossible: a Meta location
cannot be constructed without a non-empty meta_type. GoogleGeoLocation normalizes
google_id to the geoTargetConstants/ resource name Google Ads expects.
"""
import unittest

import pydantic

from app.agents.adzump.services.geo.models import (
    GeoLocationBase,
    GoogleGeoLocation,
    MetaGeoLocation,
)


class MetaGeoLocationTests(unittest.TestCase):
    def test_meta_type_is_required(self):
        with self.assertRaises(pydantic.ValidationError):
            MetaGeoLocation(name="Bandra", meta_key="123")

    def test_empty_meta_type_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            MetaGeoLocation(name="Bandra", meta_type="")

    def test_valid_location_keeps_fields(self):
        m = MetaGeoLocation(name="Bandra", meta_type="city", meta_key="123")
        self.assertEqual(m.meta_type, "city")
        self.assertEqual(m.meta_key, "123")

    def test_key_optional(self):
        # No Meta match → typed location with type but no key (radial fallback).
        m = MetaGeoLocation(name="Nowhere", meta_type="city")
        self.assertIsNone(m.meta_key)

    def test_extra_fields_preserved_on_round_trip(self):
        # Pipeline-accreted keys (distance_km, place_id, geo_level, unknowns)
        # survive a model round-trip rather than being silently dropped.
        m = MetaGeoLocation(
            name="Goa", meta_type="region", place_id="p1", distance_km=3.0, custom="x"
        )
        dumped = m.model_dump(exclude_none=True)
        self.assertEqual(dumped["place_id"], "p1")
        self.assertEqual(dumped["distance_km"], 3.0)
        self.assertEqual(dumped["custom"], "x")


class GoogleGeoLocationTests(unittest.TestCase):
    def test_bare_id_normalized_to_resource_name(self):
        g = GoogleGeoLocation(name="X", google_id="1007785")
        self.assertEqual(g.google_id, "geoTargetConstants/1007785")

    def test_already_resource_name_unchanged(self):
        g = GoogleGeoLocation(name="X", google_id="geoTargetConstants/1007785")
        self.assertEqual(g.google_id, "geoTargetConstants/1007785")

    def test_id_optional(self):
        g = GoogleGeoLocation(name="X")
        self.assertIsNone(g.google_id)


class GeoLocationBaseTests(unittest.TestCase):
    def test_defaults_are_lenient(self):
        # Discovery hands over only the fields it found; the rest default.
        b = GeoLocationBase(name="Indiranagar")
        self.assertEqual(b.city, "")
        self.assertEqual(b.distance_km, 0.0)
        self.assertIsNone(b.lat)


if __name__ == "__main__":
    unittest.main()
