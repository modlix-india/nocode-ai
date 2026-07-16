"""Unit: creative_intelligence/sources/adlibrary.py - raw ad -> Creative mapping.

The adapter's whole job is turning the vendor's live field names into a Creative.
These lock the media-type branches (image/video/video2pic/carousel), the
poster-vs-asset placement, and the last_seen active heuristic - no network.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.agents.adzump.creative_intelligence.sources.adlibrary import AdLibrarySource


def _unix(days_ago: int) -> int:
    return int((datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp())


class ToCreativeTests(unittest.TestCase):
    def setUp(self):
        self.src = AdLibrarySource()

    def test_image_uses_resource_as_asset_no_poster(self):
        c = self.src._to_creative({
            "ad_key": "k1", "ads_type": 1, "title": "Buy now",
            "resource_urls": ["full.jpg"], "preview_img_url": "prev.jpg",
        })
        self.assertEqual(c.media_type, "image")
        self.assertEqual(c.source_asset_url, "full.jpg")
        self.assertEqual(c.poster_source_url, "")
        self.assertEqual(c.headline, "Buy now")

    def test_video_asset_and_poster(self):
        c = self.src._to_creative({
            "ad_key": "k2", "ads_type": 2,
            "resource_urls": ["clip.mp4"], "preview_img_url": "still.jpg",
        })
        self.assertEqual(c.media_type, "video")
        self.assertEqual(c.source_asset_url, "clip.mp4")
        self.assertEqual(c.poster_source_url, "still.jpg")

    def test_video2pic_falls_back_to_preview_for_asset(self):
        c = self.src._to_creative({
            "ad_key": "k3", "ads_type": 2, "resource_urls": [], "preview_img_url": "still.jpg",
        })
        self.assertEqual(c.media_type, "video")
        self.assertEqual(c.source_asset_url, "still.jpg")
        self.assertEqual(c.poster_source_url, "still.jpg")

    def test_unknown_type_defaults_image(self):
        c = self.src._to_creative({"ad_key": "k4", "ads_type": 99, "preview_img_url": "p.jpg"})
        self.assertEqual(c.media_type, "image")

    def test_active_by_last_seen_window(self):
        recent = self.src._to_creative({"ad_key": "a", "ads_type": 1, "last_seen": _unix(2)})
        stale = self.src._to_creative({"ad_key": "b", "ads_type": 1, "last_seen": _unix(30)})
        self.assertTrue(recent.is_active)
        self.assertFalse(stale.is_active)


if __name__ == "__main__":
    unittest.main()
