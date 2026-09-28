"""Unit: stores/products.py - the product row writes, against a real MySQL.

Analysis creates the row (insert_product never overwrites); after that each
change writes only its own fields (update_product_fields), so two chats
changing different fields both land.
"""
from __future__ import annotations

import os
import unittest

from app.agents.adzump.models.product import Product
from app.agents.adzump.stores import products


@unittest.skipUnless(os.environ.get("ADZUMP_MYSQL_TESTS"),
                     "set ADZUMP_MYSQL_TESTS=1 to run against local MySQL")
class ProductWritesMySQLTests(unittest.IsolatedAsyncioTestCase):
    """Scratch rows live under client ZZSQLTEST, removed after each test."""

    CC = "ZZSQLTEST"

    async def asyncSetUp(self):
        from app.config import settings
        from app.db import connection
        settings.MYSQL_URL = os.environ.get("ADZUMP_MYSQL_URL", "jdbc:mysql://127.0.0.1:15001/ai")
        settings.MYSQL_USERNAME = os.environ.get("ADZUMP_MYSQL_USER", "root")
        settings.MYSQL_PASSWORD = os.environ.get("ADZUMP_MYSQL_PASSWORD", "root")
        await connection.init_db_pool()
        self.url = f"https://{self._testMethodName.replace('_', '-')}.example"
        self.pid = await products.insert_product(self.CC, self.url, Product(
            product_name="Scratch", place={"address": "Hebbal"},
            ad_accounts={"google": {"account": "111"}}))

    async def asyncTearDown(self):
        from app.db import connection
        await connection.execute_query(
            "DELETE FROM adzump_products WHERE client_code=%s", (self.CC,))
        await connection.close_db_pool()

    async def _stored(self) -> Product:
        return await products.get_product(self.CC, self.url)

    async def test_insert_never_overwrites(self):
        again = await products.insert_product(self.CC, self.url, Product(product_name="Other"))
        self.assertEqual(again, self.pid)
        self.assertEqual((await self._stored()).product_name, "Scratch")

    async def test_update_writes_only_its_fields(self):
        await products.update_product_fields(
            self.CC, self.pid, {"ad_accounts.meta": {"account": "act_1"}})
        await products.update_product_fields(
            self.CC, self.pid, {"place": {"address": "Whitefield", "country_code": "IN"}})
        stored = await self._stored()
        self.assertEqual(stored.ad_accounts["google"].account, "111")  # the other platform stays
        self.assertEqual(stored.ad_accounts["meta"].account, "act_1")
        self.assertEqual((stored.place.address, stored.product_name), ("Whitefield", "Scratch"))
        from app.db import connection
        rows = await connection.execute_query(
            "SELECT country_code FROM adzump_products WHERE id=%s", (self.pid,))
        self.assertEqual(rows[0]["country_code"], "IN")  # the column follows the data

    async def test_missing_row(self):
        self.assertFalse(await products.update_product_fields(self.CC, 0, {"place": {}}))


if __name__ == "__main__":
    unittest.main()
