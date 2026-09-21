"""Product model ↔ storage contract.

The Product model (app/agents/adzump/models/product.py) is the written-down
schema of session_ctx["product_data"]. MySQL persists it as a typed
model_dump/model_validate round trip (identity by construction); what needs
locking is the OUTBOUND projection `_build_full_record` - the Modlix mirror
record DS launch-time agents read.
"""
from __future__ import annotations

import unittest
from unittest import mock

from app.agents.adzump.models.product import check_product
from app.agents.adzump.services.business_storage import _build_full_record


class MirrorRecordProjectionTests(unittest.TestCase):
    """product_data → _build_full_record: the ds-side contract fields."""

    def test_mirror_record_projects_ds_contract(self):
        session_ctx = {
            "product_data": {
                "product_name": "Sumadhura Solea",
                "business_type": "real estate",
                "business_scale": "local",
                "summary": "Luxury 3 & 4 BHK apartments.",
                "primary_url": "https://dahliasgurgaon.com/",
                "pages": {"https://dahliasgurgaon.com/":
                          {"screenshot_url": "https://cdn/x.png"}},
                "assets": {
                    "logos": [{"url": "https://cdn/logo.png", "source": "scrape",
                               "confidence": 0.9}],
                    "images": [{"url": "https://cdn/c1.png",
                                "display": {"fit": "cover"},
                                "role": "hero", "source": "site_pick"}],
                },
                "target_areas": [{"name": "Whitefield", "lat": 12.96, "lng": 77.75,
                                  "distance_km": 5.0,
                                  "meta": {"type": "city", "key": "777",
                                           "name": "Whitefield"}}],
                "place": {"address": "Bengaluru", "lat": 12.96, "lng": 77.75,
                          "country_code": "IN",
                          "country_geo_constant": "geoTargetConstants/2356",
                          "display_name": "Sumadhura Solea, Bengaluru"},
            },
            "campaign_spec": {"platform": "Meta", "location": "Bengaluru"},
        }

        record = _build_full_record(session_ctx, "https://dahliasgurgaon.com/")

        self.assertEqual(record["productName"], "Sumadhura Solea")
        self.assertEqual(record["screenshot"], "https://cdn/x.png")
        self.assertEqual(record["logoUrl"], "https://cdn/logo.png")
        self.assertEqual(record["creativeImages"], ["https://cdn/c1.png"])
        # The per-platform mapped-location key (the original bug) must project.
        self.assertEqual(
            record["campaign"]["metaMappedLocations"][0]["meta"]["key"], "777")
        self.assertEqual(record["campaign"]["googleMappedLocations"], [])
        # country_code + geo constant (scope geo lookups) must project.
        self.assertEqual(record["campaign"]["location"]["country_code"], "IN")
        self.assertEqual(
            record["campaign"]["location"]["country_geo_constant"],
            "geoTargetConstants/2356")


class CheckProductTests(unittest.TestCase):
    """check_product - the warn-only runtime boundary check."""

    def test_valid_product_logs_nothing(self):
        with mock.patch("app.agents.adzump.models.product.logger") as log:
            check_product({"product_name": "X"}, where="test")
        log.warning.assert_not_called()

    def test_unknown_keys_warn_but_never_raise(self):
        with mock.patch("app.agents.adzump.models.product.logger") as log:
            check_product({"product_name": "X", "brand_new_key": 1}, where="test")
        self.assertIn("product_schema_unknown_keys", log.warning.call_args.args[0])

    def test_wrong_shape_warns_but_never_raises(self):
        with mock.patch("app.agents.adzump.models.product.logger") as log:
            check_product({"pages": "not-a-dict"}, where="test")
        self.assertIn("product_schema_drift", log.warning.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
