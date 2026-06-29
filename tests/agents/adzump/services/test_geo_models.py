"""Geo-targeting location models — the typed contract for platform locations.

MetaGeoLocation makes the original bug structurally impossible: a Meta location
cannot be constructed without a non-empty type. GoogleGeoLocation normalizes id to
the geoTargetConstants/ resource name. TargetArea composes the generic 'where'
with at most one platform handle — so a mapped location carries the scale once and
the platform type once, never a duplicated meta_type + geo_level pair.
"""
import unittest

import pydantic

from app.agents.adzump.services.geo.models import (
    GoogleGeoLocation,
    MetaGeoLocation,
    TargetArea,
)


class MetaGeoLocationTests(unittest.TestCase):
    def test_type_is_required(self):
        with self.assertRaises(pydantic.ValidationError):
            MetaGeoLocation(key="123")

    def test_empty_type_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            MetaGeoLocation(type="")

    def test_valid_keeps_fields(self):
        m = MetaGeoLocation(type="city", key="123", name="Bandra")
        self.assertEqual((m.type, m.key, m.name), ("city", "123", "Bandra"))

    def test_key_optional(self):
        # No Meta match → typed handle with type but no key (radial fallback).
        m = MetaGeoLocation(type="city")
        self.assertIsNone(m.key)

    def test_carries_only_platform_params(self):
        # The model rejects generic geo fields leaking in — platform-only.
        self.assertEqual(set(MetaGeoLocation.model_fields), {"type", "key", "name"})


class GoogleGeoLocationTests(unittest.TestCase):
    def test_bare_id_normalized_to_resource_name(self):
        g = GoogleGeoLocation(id="1007785")
        self.assertEqual(g.id, "geoTargetConstants/1007785")

    def test_already_resource_name_unchanged(self):
        g = GoogleGeoLocation(id="geoTargetConstants/1007785")
        self.assertEqual(g.id, "geoTargetConstants/1007785")

    def test_id_optional(self):
        self.assertIsNone(GoogleGeoLocation().id)

    def test_carries_only_platform_params(self):
        self.assertEqual(set(GoogleGeoLocation.model_fields), {"id", "name"})


class TargetAreaTests(unittest.TestCase):
    def test_defaults_are_lenient(self):
        a = TargetArea(name="Indiranagar")
        self.assertEqual(a.city, "")
        self.assertEqual(a.distance_km, 0.0)
        self.assertIsNone(a.lat)
        self.assertIsNone(a.meta)
        self.assertIsNone(a.google)

    def test_nests_platform_handle_and_dumps_clean(self):
        a = TargetArea(
            name="India", lat=22.0, lng=79.0, scale="country",
            meta=MetaGeoLocation(type="country", key="IN", name="India"),
        )
        dumped = a.model_dump(exclude_none=True)
        self.assertEqual(dumped["scale"], "country")
        self.assertEqual(dumped["meta"], {"type": "country", "key": "IN", "name": "India"})
        self.assertNotIn("google", dumped)        # unset platform omitted
        self.assertNotIn("meta_type", dumped)      # no flat/duplicated type

    def test_dict_meta_coerced_to_model(self):
        a = TargetArea(name="X", meta={"type": "city", "key": "1"})
        self.assertIsInstance(a.meta, MetaGeoLocation)
        self.assertEqual(a.meta.type, "city")


if __name__ == "__main__":
    unittest.main()
