"""Unit: stores/competitors.py - competitor identity keys + row mapping.

A competitor's key is its canonical website (host + path), clamped to the url
column so the key a read looks up is exactly what both writers store; a
website-less competitor keys by name.
"""
from __future__ import annotations

import json
import os
import unittest

from app.agents.adzump.creative_intelligence.models import (
    Competitor, Creative, Essence, Rendition,
)
from app.agents.adzump.stores import competitors


class CompetitorKeyTests(unittest.TestCase):
    def test_identity_keys(self):
        # Identity = canonical website, path included: project pages on one
        # developer site are separate competitors (Kailash 2026-09-23).
        long_path = "https://x.com/" + "a" * 600
        for raw, expected in [
            ("https://www.Nike.com/air", "https://nike.com/air"),
            ("nike.com", "https://nike.com"),
            ("http://uk.gymshark.com/", "https://uk.gymshark.com"),
            ("WWW.Example.COM", "https://example.com"),
            ("https://brigadegroup.com/p/avalon?utm_source=x#top",
             "https://brigadegroup.com/p/avalon"),
            ("", ""),
            ("   ", ""),
            (long_path, long_path[:512]),
        ]:
            with self.subTest(key=raw[:40] or repr(raw)):
                self.assertEqual(competitors.competitor_key(raw), expected)
                self.assertEqual(competitors._website(raw), expected or None)
        for name, expected in [("Nambiar Villas", "name:nambiar-villas"),
                               ("  ", "")]:
            with self.subTest(name_key=name):
                self.assertEqual(competitors.name_key(name), expected)


class RowToCreativeTests(unittest.TestCase):
    def test_renditions_survive_the_read(self):
        # Reads used to drop content["renditions"], so any read->write round
        # trip (augment merge, repair sweep) lost the placement versions.
        rendition = {"fileUrl": "https://f/wide.jpg", "width": 1910, "height": 1000,
                     "aspectRatio": 1.91, "contentHash": "h", "perceptualHash": "p"}
        row = {"content": {"creativeId": "c1", "renditions": [rendition]},
               "format": "single", "is_active": 1, "days_running": 4}
        creative = competitors._row_to_creative(row, None)
        self.assertEqual([r.model_dump(by_alias=True) for r in creative.renditions],
                         [rendition])


class CreativeRoundTripTests(unittest.TestCase):
    # Vendor-side transients the store never keeps (the rehosted copy replaces them).
    NOT_STORED = {"source_asset_url", "poster_source_url", "poster_width", "poster_height"}

    def test_creative_survives_write_then_read(self):
        # Written exactly as sync_competitor writes it (content blob + slide-0
        # asset), then read back: essence is paid vision, renditions are
        # placement versions - a field lost here is lost on every refresh.
        image = Creative(
            creative_id="ad-1", media_type="image", file_url="https://f/ad-1.jpg",
            content_hash="c1", perceptual_hash="p1", headline="Lake-view villas",
            primary_text="From 2.1 Cr", description="RERA approved", cta="Learn more",
            landing_url="https://brigade.com/avalon", platform="meta", format="image",
            publisher_platforms=["facebook", "instagram"], first_seen="2026-09-01",
            last_seen="2026-09-20", is_active=True, days_running=19, variants=3,
            width=1080, height=1350, aspect_ratio=1080 / 1350,
            verified_at="2026-09-20T10:00:00+00:00",
            essence=Essence(angle="lakeside living", hook_type="aspiration",
                            category="residential_villa"),
            renditions=[Rendition(file_url="https://f/ad-1-story.jpg", width=1080,
                                  height=1920, aspect_ratio=1080 / 1920,
                                  content_hash="c2", perceptual_hash="p2")],
        )
        video = Creative(
            creative_id="ad-2", media_type="video", file_url="https://f/ad-2.mp4",
            poster_url="https://f/ad-2.jpg", width=720, height=1280,
            aspect_ratio=720 / 1280, duration_seconds=14.5, is_active=False,
        )
        for creative in (image, video):
            with self.subTest(creative.creative_id):
                row = {"content": json.dumps(competitors._creative_content(creative)),
                       "format": competitors._creative_format(creative.media_type),
                       "is_active": int(creative.is_active),
                       "days_running": creative.days_running}
                read = competitors._row_to_creative(row, competitors._primary_asset(creative))
                self.assertEqual(read.model_dump(exclude=self.NOT_STORED),
                                 creative.model_dump(exclude=self.NOT_STORED))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(os.environ.get("ADZUMP_MYSQL_TESTS"),
                     "set ADZUMP_MYSQL_TESTS=1 to run against local MySQL")
class CompetitorWritesMySQLTests(unittest.IsolatedAsyncioTestCase):
    """The single-row writes against a real MySQL (local 15001 by default):
    landing on a saved row, update order and unique keys are MySQL behaviour
    mocks can't check. Scratch rows live under client ZZSQLTEST, removed after."""

    CC = "ZZSQLTEST"

    async def asyncSetUp(self):
        from app.config import settings
        from app.db import connection
        from app.agents.adzump.models.product import Product
        from app.agents.adzump.stores import products
        settings.MYSQL_URL = os.environ.get("ADZUMP_MYSQL_URL", "jdbc:mysql://127.0.0.1:15001/ai")
        settings.MYSQL_USERNAME = os.environ.get("ADZUMP_MYSQL_USER", "root")
        settings.MYSQL_PASSWORD = os.environ.get("ADZUMP_MYSQL_PASSWORD", "root")
        await connection.init_db_pool()
        self.url = f"https://{self._testMethodName.replace('_', '-')}.example"
        self.pid = await products.insert_product(self.CC, self.url, Product(product_name="Scratch"))

    async def asyncTearDown(self):
        from app.db import connection
        await connection.execute_query(  # competitors and their ads cascade
            "DELETE FROM adzump_products WHERE client_code=%s", (self.CC,))
        await connection.close_db_pool()

    async def _row(self, row_id):
        from app.db import connection
        rows = await connection.execute_query(
            "SELECT name, url, url_source, status, creative_status, location, pricing "
            "FROM adzump_competitors WHERE id=%s", (row_id,))
        return rows[0]

    async def _add(self, profile, revive=False):
        return await competitors.add_competitor(self.CC, self.pid, profile, revive=revive)

    async def test_add_lands_on_the_saved_row(self):
        sobha = await self._add({"name": "Sobha Magnus", "url": "https://sobha.com/magnus"})
        rows = [  # (case, profile, same row)
            ("same name", {"name": "Sobha Magnus"}, True),
            ("same website, another name", {"name": "Sobha Magnus Phase 2",
                                            "url": "https://www.sobha.com/magnus/"}, True),
            ("new competitor", {"name": "Prestige Lakeside"}, False),
        ]
        for case, profile, same in rows:
            with self.subTest(case):
                self.assertEqual(await self._add(profile) == sobha, same)
        self.assertEqual((await self._row(sobha))["name"], "Sobha Magnus")  # never renamed

    async def test_only_the_user_brings_back_a_deleted_row(self):
        row_id = await self._add({"name": "Sobha Magnus"})
        await competitors.delete_competitor(self.CC, self.pid, row_id)
        self.assertIsNone(await self._add({"name": "Sobha Magnus"}))  # research
        self.assertEqual((await self._row(row_id))["status"], "deleted")
        self.assertEqual(await self._add({"name": "Sobha Magnus"}, revive=True), row_id)
        self.assertEqual((await self._row(row_id))["status"], "active")

    async def test_a_landing_fills_only_empty_fields(self):
        row_id = await self._add({"name": "Sobha Magnus", "location": "Hebbal"})
        await self._add({"name": "Sobha Magnus", "location": "Whitefield", "pricing": "2 Cr"})
        await competitors.fill_competitor_profile(
            self.CC, self.pid, row_id, {"location": "Sarjapur", "pricing": "3 Cr"})
        row = await self._row(row_id)
        self.assertEqual((row["location"], row["pricing"]), ("Hebbal", "2 Cr"))

    async def test_set_competitor_website(self):
        sobha = await self._add({"name": "Sobha Magnus"})
        prestige = await self._add({"name": "Prestige Lakeside", "url": "https://prestige.com/lake"})
        taken = await competitors.set_competitor_website(
            self.CC, self.pid, sobha, "https://prestige.com/lake", "user",
            reset_ads=True, keep_user_pin=False)
        self.assertFalse(taken)
        self.assertTrue(await competitors.set_competitor_website(
            self.CC, self.pid, sobha, "https://sobha.com/magnus", "user",
            reset_ads=True, keep_user_pin=False))
        row = await self._row(sobha)
        self.assertEqual((row["url"], row["url_source"], row["creative_status"]),
                         ("https://sobha.com/magnus", "user", "pending"))
        pin_held = await competitors.set_competitor_website(
            self.CC, self.pid, sobha, "https://sobha.com/other", None,
            reset_ads=False, keep_user_pin=True)
        self.assertFalse(pin_held)
        self.assertEqual((await self._row(prestige))["url"], "https://prestige.com/lake")

    async def test_ads_write_never_moves_a_website(self):
        row_id = await self._add({"name": "Sobha Magnus", "url": "https://sobha.com/magnus"})
        record = Competitor(competitor_key=competitors.name_key("Sobha Magnus"),
                            name="Sobha Magnus")
        self.assertEqual(await competitors.sync_competitor(self.CC, self.url, record), row_id)
        self.assertEqual((await self._row(row_id))["url"], "https://sobha.com/magnus")
