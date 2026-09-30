"""Geo-targeting location models - the typed contract for platform locations.

MetaGeoLocation makes the original bug structurally impossible: a Meta location
cannot be constructed without a non-empty type AND key (Meta targeting rejects a
keyless entry). GoogleGeoLocation likewise requires a non-empty resourceName and
normalizes a bare id to the geoTargetConstants/ resource name. How a TargetArea
carries one platform handle is tested with the mapper (test_platform_mapping).
"""
import unittest

import pydantic

from app.agents.adzump.agents.location.models import (
    AddLocation,
    GoogleGeoLocation,
    MetaGeoLocation,
    TargetArea,
)


class PlatformHandleTests(unittest.TestCase):
    def test_a_handle_needs_its_platform_ids(self):
        # Meta rejects keyless entries and Google needs a resolved constant: with
        # no match the mapper attaches no handle at all.
        for label, build in [
            ("meta without a type", lambda: MetaGeoLocation(key="123")),
            ("meta with an empty type", lambda: MetaGeoLocation(type="", key="123")),
            ("meta without a key", lambda: MetaGeoLocation(type="city")),
            ("meta with an empty key", lambda: MetaGeoLocation(type="city", key="")),
            ("google without a resource name", lambda: GoogleGeoLocation()),
            ("google with an empty resource name", lambda: GoogleGeoLocation(resourceName="")),
        ]:
            with self.subTest(label), self.assertRaises(pydantic.ValidationError):
                build()

    def test_google_id_normalized_to_resource_name(self):
        for raw in ("1007785", "geoTargetConstants/1007785"):
            with self.subTest(raw):
                self.assertEqual(GoogleGeoLocation(resourceName=raw).resourceName,
                                 "geoTargetConstants/1007785")


class ScaleVocabularyTests(unittest.TestCase):
    """scale is a closed Literal (PR #91 B6): every accepted value must be a
    broad scale the pincode-backfill exemption knows, so the vocabulary can't
    drift into a value that gets its map polygon pincode-shrunk."""

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
