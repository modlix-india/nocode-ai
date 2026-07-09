"""Product model ↔ storage contract.

The Product model (app/agents/adzump/models/product.py) is the written-down schema of
session_ctx["product_data"]. It is not yet enforced at runtime - these tests
are what make it load-bearing: the storage restore path must produce a dict
the model fully understands. A new key added to _record_to_business without a
matching Product field fails here (schema drift caught at test time, not in a
live session).
"""
from __future__ import annotations

import unittest
from unittest import mock

from app.agents.adzump._shared import primary_screenshot_url
from app.agents.adzump.models import Place
from app.agents.adzump.models.product import Product, check_product
from app.agents.adzump.services.business_storage import (
    _build_full_record,
    _record_to_business,
)


def _record(data: dict) -> dict:
    """Wrap raw AISuggestedData fields the way storage records arrive."""
    return {"data": data}


class RestorePathContractTests(unittest.TestCase):
    def test_empty_record_restores_to_valid_product(self):
        product = Product.model_validate(_record_to_business(_record({})))
        # Every restore key must be a declared field - extras mean the
        # restore path and the model have drifted apart.
        self.assertFalse(
            product.model_extra,
            f"undeclared product_data keys from restore: {list(product.model_extra)}",
        )

    def test_full_record_restores_to_valid_product(self):
        product = Product.model_validate(_record_to_business(_record({
            "businessUrl": "https://x.com",
            "productName": "Sumadhura Solea",
            "businessType": "real estate",
            "businessScale": "local",
            "summary": "Luxury 3 & 4 BHK apartments.",
            "location": {"product_location": "Bengaluru", "area_location": "Whitefield"},
            "screenshot": "https://cdn/x.png",
            # Legacy ds-v1 contact carries address - restore drops it (place owns location).
            "contact": {"phone": "080-123", "email": "a@b.com", "address": "MG Road"},
            "logoUrl": "https://cdn/logo.png",
            "logoMeta": {"source": "scrape", "reasoning": "header img",
                         "confidence": 0.9, "display": {"bg": "light"}},
            "creativeImages": ["https://cdn/c1.png"],
            "siteLinks": [{"text": "A", "href": "https://x.com/a"}],
            "scrapedUrls": ["https://x.com"],
            "scrapeCount": 2,
            "campaign": {
                "targetAreas": [{"name": "Whitefield", "lat": 12.96, "lng": 77.75,
                                 "distance_km": 5.0,
                                 "meta": {"type": "city", "key": "777", "name": "Whitefield"}}],
                "location": {"lat": 12.96, "lng": 77.75},
            },
        })))
        self.assertFalse(product.model_extra)
        # Typed access works end-to-end, including the nested platform handle
        # and the hydrated location string.
        self.assertEqual(
            product.place, Place(address="Bengaluru", lat=12.96, lng=77.75))
        self.assertEqual(product.target_areas[0].meta.type, "city")
        self.assertEqual(product.target_areas[0].meta.key, "777")
        self.assertEqual(product.assets.logos[0].url, "https://cdn/logo.png")
        self.assertEqual(product.assets.logos[0].confidence, 0.9)
        self.assertEqual(product.assets.images[0].url, "https://cdn/c1.png")
        self.assertEqual(product.contact.phone, "080-123")
        # Stored "screenshot" (ds contract) re-attaches to the primary page;
        # the session-side value is derived, never stored twice.
        self.assertEqual(product.primary_url, "https://x.com")
        self.assertEqual(product.pages["https://x.com"].screenshot_url,
                         "https://cdn/x.png")
        self.assertEqual(primary_screenshot_url(product.model_dump()),
                         "https://cdn/x.png")


class SaveRestoreRoundTripTests(unittest.TestCase):
    """product_data → _build_full_record → _record_to_business → Product.

    Locks that a launched campaign's product state survives a session restart.
    Known one-way losses (save never writes them, NOT asserted here):
    non-primary logos (only logos[0] persists), image role/source,
    non-primary page screenshots.
    """

    def test_campaign_state_survives_round_trip(self):
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
            },
            "campaign_spec": {"platform": "Meta", "location": "Bengaluru"},
            "_location_meta": {"lat": 12.96, "lng": 77.75, "address": "Bengaluru"},
        }

        record = _build_full_record(session_ctx, "https://dahliasgurgaon.com/")
        restored = Product.model_validate(_record_to_business({"data": record}))

        self.assertFalse(restored.model_extra)
        self.assertEqual(restored.product_name, "Sumadhura Solea")
        self.assertEqual(restored.business_scale, "local")
        # The Meta handle - the original bug - must survive the round trip.
        self.assertEqual(restored.target_areas[0].meta.type, "city")
        self.assertEqual(restored.target_areas[0].meta.key, "777")
        # ds-side contract: the stored record still projects the per-platform key.
        self.assertEqual(
            record["campaign"]["metaMappedLocations"][0]["meta"]["key"], "777")
        self.assertEqual(record["campaign"]["googleMappedLocations"], [])
        # The screenshot survives on the restored primary page (derived read).
        self.assertEqual(primary_screenshot_url(restored.model_dump()),
                         "https://cdn/x.png")
        self.assertEqual(restored.assets.logos[0].url, "https://cdn/logo.png")
        self.assertEqual(restored.assets.logos[0].confidence, 0.9)
        self.assertEqual(restored.assets.images[0].url, "https://cdn/c1.png")
        self.assertEqual(restored.assets.images[0].display, {"fit": "cover"})
        self.assertEqual(
            restored.place, Place(address="Bengaluru", lat=12.96, lng=77.75))


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
