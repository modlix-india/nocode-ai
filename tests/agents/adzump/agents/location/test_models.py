"""Geo-targeting location models - the typed contract for platform locations.

MetaGeoLocation makes the original bug structurally impossible: a Meta location
cannot be constructed without a non-empty type AND key (Meta targeting rejects a
keyless entry). GoogleGeoLocation likewise requires a non-empty resourceName and
normalizes a bare id to the geoTargetConstants/ resource name. TargetArea composes
the generic 'where'
with at most one platform handle - so a mapped location carries the scale once and
the platform type once, never a duplicated type + geo_level pair.
"""
import unittest

import pydantic

from app.agents.adzump.agents.location.models import (
    AddLocation,
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

    def test_key_required(self):
        # A typed-but-keyless handle is rejected by Meta, so the model forbids
        # it: no match → the mapper attaches no handle at all (radius fallback).
        with self.assertRaises(pydantic.ValidationError):
            MetaGeoLocation(type="city")

    def test_empty_key_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            MetaGeoLocation(type="city", key="")

    def test_carries_only_platform_params(self):
        # The model rejects generic geo fields leaking in - platform-only.
        self.assertEqual(set(MetaGeoLocation.model_fields), {"type", "key", "name"})


class GoogleGeoLocationTests(unittest.TestCase):
    def test_bare_id_normalized_to_resource_name(self):
        g = GoogleGeoLocation(resourceName="1007785")
        self.assertEqual(g.resourceName, "geoTargetConstants/1007785")

    def test_already_resource_name_unchanged(self):
        g = GoogleGeoLocation(resourceName="geoTargetConstants/1007785")
        self.assertEqual(g.resourceName, "geoTargetConstants/1007785")

    def test_resource_name_required(self):
        # A handle without a geo-target constant is meaningless, so the model
        # forbids it: no constant resolved → no handle (lat/lng proximity).
        with self.assertRaises(pydantic.ValidationError):
            GoogleGeoLocation()
        with self.assertRaises(pydantic.ValidationError):
            GoogleGeoLocation(resourceName="")

    def test_carries_only_platform_params(self):
        self.assertEqual(set(GoogleGeoLocation.model_fields), {"resourceName", "name"})


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
        self.assertNotIn("type", dumped)            # no flat/duplicated type

    def test_dict_meta_coerced_to_model(self):
        a = TargetArea(name="X", meta={"type": "city", "key": "1"})
        self.assertIsInstance(a.meta, MetaGeoLocation)
        self.assertEqual(a.meta.type, "city")


class ScaleVocabularyTests(unittest.TestCase):
    """scale is a closed Literal (PR #91 B6): every accepted value must be a
    broad scale the pincode-backfill exemption knows, so the vocabulary can't
    drift into a value that gets its map polygon pincode-shrunk."""

    def test_every_scale_value_accepted(self):
        for scale in ("city", "state", "region", "country"):
            with self.subTest(scale):
                self.assertEqual(TargetArea(name="X", scale=scale).scale, scale)
                self.assertEqual(AddLocation(name="X", scale=scale).scale, scale)

    def test_out_of_vocabulary_scale_rejected(self):
        for model_cls in (TargetArea, AddLocation):
            with self.subTest(model_cls.__name__):
                with self.assertRaises(pydantic.ValidationError):
                    model_cls(name="X", scale="metro")

    def test_broad_scales_derived_from_vocabulary(self):
        from app.agents.adzump.agents.location.models import Scale
        from app.agents.adzump.agents.location.platform_mapping import BROAD_SCALES
        from typing import get_args
        self.assertEqual(BROAD_SCALES, set(get_args(Scale)))


if __name__ == "__main__":
    unittest.main()
