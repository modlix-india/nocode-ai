"""Unit: products_router - the UI's product library reads + deletes.

Every route scopes its store call to the caller's client_code (another
client's id must read as a 404, never leak), deletes stamp the caller as
updated_by, and responses are snake_case throughout, the nested Creative
included.
"""
from __future__ import annotations

import types
import unittest
from datetime import datetime
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents.adzump import products_router
from app.agents.adzump.creative_intelligence.models import Creative, Rendition
from app.agents.adzump.models.product import Product
from app.core.base_auth import require_auth_context

CLIENT = "GRMEL"
USER = 11
STORES = "app.agents.adzump.stores"


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(products_router.router)
    app.dependency_overrides[require_auth_context] = (
        lambda: types.SimpleNamespace(client_code=CLIENT, user_id=USER))
    return TestClient(app)


class ProductsRouterTests(unittest.TestCase):
    def test_every_route_scopes_to_the_caller_client(self):
        rows = [  # (method, path, store fn, returns, expected store args)
            ("get", "/products", "products.list_products", [], (CLIENT,)),
            ("get", "/products/7", "products.get_product_by_id",
             ("https://a.com", Product()), (CLIENT, 7)),
            ("delete", "/products/7", "products.delete_product", True, (CLIENT, 7)),
            ("get", "/products/7/competitors",
             "competitors.list_product_competitors", [], (CLIENT, 7)),
            ("delete", "/products/7/competitors/3",
             "competitors.delete_competitor", True, (CLIENT, 7, 3, USER)),
            ("delete", "/products/7/competitors/3/creatives/ad-9",
             "competitors.delete_creative", True, (CLIENT, 7, 3, "ad-9", USER)),
            ("get", "/products/7/creatives",
             "competitors.list_product_creatives", {}, (CLIENT, 7, None)),
            ("get", "/products/7/creatives?competitor_id=3",
             "competitors.list_product_creatives", {}, (CLIENT, 7, 3)),
        ]
        for method, path, fn, returns, args in rows:
            with self.subTest(f"{method} {path}"), mock.patch(
                    f"{STORES}.{fn}", new=mock.AsyncMock(return_value=returns)) as store:
                response = getattr(_client(), method)(path)
                self.assertLess(response.status_code, 300, response.text)
                store.assert_awaited_once_with(*args)

    def test_unknown_product_is_404(self):
        rows = [("get", "/products/9", "products.get_product_by_id", None),
                ("delete", "/products/9", "products.delete_product", False),
                ("delete", "/products/9/competitors/3", "competitors.delete_competitor", False),
                ("delete", "/products/9/competitors/3/creatives/ad-9",
                 "competitors.delete_creative", False)]
        for method, path, fn, miss in rows:
            with self.subTest(f"{method} {path}"), mock.patch(
                    f"{STORES}.{fn}", new=mock.AsyncMock(return_value=miss)):
                self.assertEqual(getattr(_client(), method)(path).status_code, 404)

    def test_delete_returns_no_content(self):
        with mock.patch(f"{STORES}.products.delete_product",
                        new=mock.AsyncMock(return_value=True)):
            response = _client().delete("/products/7")
        self.assertEqual((response.status_code, response.content), (204, b""))

    def test_listings_serialize_rows(self):
        product = {"id": 7, "url": "https://a.com", "name": "A", "category": None,
                   "country_code": "IN", "summary": "s",
                   "updated_at": datetime(2026, 9, 24, 10, 0)}
        competitor = {"id": 3, "name": "B", "url": None, "logo_url": None,
                      "business_type": "villas", "location": None, "pricing": None,
                      "key_usps": ["lake view"], "weakness": None,
                      "why_competitor": "same buyer", "creative_status": "pending",
                      "total_creatives": 0, "active_creatives": 0,
                      "creatives_fetched_at": None}
        with mock.patch(f"{STORES}.products.list_products",
                        new=mock.AsyncMock(return_value=[product])), \
             mock.patch(f"{STORES}.competitors.list_product_competitors",
                        new=mock.AsyncMock(return_value=[competitor])):
            products = _client().get("/products").json()
            competitors = _client().get("/products/7/competitors").json()
        self.assertEqual(products[0]["updated_at"], "2026-09-24T10:00:00")
        self.assertEqual(competitors, [competitor])

    def test_creatives_grouped_and_snake_case(self):
        creative = Creative(creative_id="c1", file_url="https://f/1.jpg",
                            renditions=[Rendition(file_url="https://f/1-wide.jpg")])
        with mock.patch(f"{STORES}.competitors.list_product_creatives",
                        new=mock.AsyncMock(return_value={3: [creative]})):
            body = _client().get("/products/7/creatives").json()
        self.assertEqual(body[0]["competitor_id"], 3)
        ad = body[0]["creatives"][0]
        self.assertEqual((ad["creative_id"], ad["file_url"]), ("c1", "https://f/1.jpg"))
        self.assertEqual(ad["renditions"][0]["file_url"], "https://f/1-wide.jpg")
        self.assertNotIn("creativeId", ad)


if __name__ == "__main__":
    unittest.main()
