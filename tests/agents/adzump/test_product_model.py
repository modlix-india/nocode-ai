"""Unit: models/product.py - the typed schema of session_ctx["product_data"].

Every field must survive the adzump_products.data JSON column, and the
warn-only boundary check must never raise. The Modlix mirror projection is
tested with product_service.
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from app.agents.adzump.agents.location.models import (
    GoogleGeoLocation, MetaGeoLocation, TargetArea,
)
from app.agents.adzump.agents.product.models import SiteLink
from app.agents.adzump.models.offer_state import OfferState
from app.agents.adzump.models.place import Place
from app.agents.adzump.models.product import (
    AdAccounts, Assets, Contact, Image, Logo, Page, Product, check_product,
)


class ProductRoundTripTests(unittest.TestCase):
    def test_every_field_survives_the_data_column(self):
        # adzump_products.data stores model_dump(mode="json"); resume validates it back.
        product = Product(
            product_name="Sumadhura Solea", business_type="real estate",
            business_scale="local", summary="brief", profile_summary="rich profile",
            place=Place(address="Whitefield", lat=12.96, lng=77.75, country_code="IN",
                        country_geo_constant="geoTargetConstants/2356",
                        display_name="Sumadhura Solea, Whitefield"),
            pricing="1.2 Cr onwards", contact=Contact(phone="+91 90000 00000", email="a@b.in"),
            unique_features=["clubhouse"], products_services=["3 BHK"],
            category="residential_apartment", subcategory="premium", market="Bengaluru",
            offering_stage="pre_launch", category_source="business_type",
            category_confidence=0.9, taxonomy_version="v2",
            category_override="residential_villa",
            primary_url="https://solea.in",
            pages={"https://solea.in": Page(screenshot_url="/f/hero.jpg")},
            pages_analyzed=["https://solea.in"],
            site_links=[SiteLink(text="Amenities", href="https://solea.in/amenities")],
            assets=Assets(
                logos=[Logo(url="/f/logo.png", display={"background": "dark"},
                            source="user_upload", source_url="https://solea.in/logo.png",
                            role="project", reasoning="lockup", format="png",
                            confidence=1.0)],
                images=[Image(url="/f/pool.jpg", display={"fit": "cover"},
                              role="amenity", source="site_pick")]),
            target_areas=[TargetArea(
                name="Whitefield", city="Bengaluru", state="Karnataka", pincode="560066",
                lat=12.96, lng=77.75, distance_km=5.0, place_id="p1", scale="city",
                meta=MetaGeoLocation(type="city", key="777", name="Whitefield"),
                google=GoogleGeoLocation(resourceName="1007768", name="Whitefield"))],
            ad_accounts={"meta": AdAccounts(
                parent_account="bm1", account="act_1", fb_page="fb1", ig_page="ig1",
                instagram=OfferState.DECLINED, names={"act_1": "Solea Ads"})},
        )
        defaulted = [name for name, field in Product.model_fields.items()
                     if getattr(product, name) == field.get_default(call_default_factory=True)]
        self.assertEqual(defaulted, [], "populate new Product fields here")
        stored = json.loads(json.dumps(product.model_dump(mode="json")))
        self.assertEqual(Product.model_validate(stored), product)


class CheckProductTests(unittest.TestCase):
    """check_product - the warn-only runtime boundary check never raises."""

    def test_rows(self):
        for label, data, warns in [
            ("a valid product", {"product_name": "X"}, False),
            ("an unknown key", {"product_name": "X", "brand_new_key": 1}, True),
            ("a wrong shape", {"pages": "not-a-dict"}, True),
        ]:
            with self.subTest(label), mock.patch(
                    "app.agents.adzump.models.product.logger") as log:
                check_product(data, where="test")
                self.assertEqual(log.warning.called, warns)


if __name__ == "__main__":
    unittest.main()
