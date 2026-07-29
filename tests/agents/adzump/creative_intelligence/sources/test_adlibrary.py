"""Unit: creative_intelligence/sources/adlibrary.py - raw ad -> Creative + search body.

Locks the vendor field mapping (media-type branches, poster-vs-asset placement,
the last_seen active heuristic) and the /search lookback window - no network.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.agents.adzump.creative_intelligence.sources import adlibrary
from app.agents.adzump.creative_intelligence.sources.adlibrary import AdLibrarySource


def _unix(days_ago: int) -> int:
    return int((datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp())


class AdLibraryTests(unittest.TestCase):
    def test_mapping_and_search_window(self):
        src = AdLibrarySource()
        for name, raw, media_type, asset, poster in [
            ("image: full-res resource, no poster",
             {"ads_type": 1, "resource_urls": ["full.jpg"], "preview_img_url": "prev.jpg"},
             "image", "full.jpg", ""),
            ("video: file + poster still",
             {"ads_type": 2, "resource_urls": ["clip.mp4"], "preview_img_url": "still.jpg"},
             "video", "clip.mp4", "still.jpg"),
            ("video2pic: still-only fallback",
             {"ads_type": 2, "resource_urls": [], "preview_img_url": "still.jpg"},
             "video", "still.jpg", "still.jpg"),
            ("unknown type defaults to image",
             {"ads_type": 99, "preview_img_url": "p.jpg"}, "image", "p.jpg", ""),
        ]:
            with self.subTest(mapping=name):
                c = src._to_creative({"ad_key": "k", **raw})
                self.assertEqual((c.media_type, c.source_asset_url, c.poster_source_url),
                                 (media_type, asset, poster))
        with self.subTest("active = last_seen within the window"):
            recent = src._to_creative({"ad_key": "a", "ads_type": 1, "last_seen": _unix(2)})
            stale = src._to_creative({"ad_key": "b", "ads_type": 1, "last_seen": _unix(30)})
            self.assertTrue(recent.is_active)
            self.assertFalse(stale.is_active)

        # regression: 2026-07-27 - daysBack=90 returned 0 of the 35 indexed
        # "Purva Sparkling Springs" ads (the vendor's crawl of the page lagged
        # ~100 days) while Meta showed the page live. The window must stay
        # wide; recency is judged downstream from per-ad timestamps.
        captured = {}

        class FakeResponse:
            status_code = 200
            def json(self):
                return {"results": [], "total": 0}

        class FakeClient:
            def __init__(self, **_kw): ...
            async def __aenter__(self): return self
            async def __aexit__(self, *_a): return False
            async def post(self, _url, headers=None, json=None):
                captured["body"] = json
                return FakeResponse()

        with self.subTest("lookback window stays year-wide"), \
                mock.patch.object(adlibrary.httpx, "AsyncClient", FakeClient), \
                mock.patch.object(adlibrary.settings, "ADLIBRARY_API_KEY", "k"):
            asyncio.run(AdLibrarySource()._search(keyword="x", page=1, page_size=10))
            self.assertGreaterEqual(captured["body"]["daysBack"], 365)


if __name__ == "__main__":
    unittest.main()
