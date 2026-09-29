"""Unit: stores/competitors.py - competitor identity keys + row mapping.

A competitor's key is its canonical website (host + path), clamped to the url
column so the key a read looks up is exactly what both writers store; a
website-less competitor keys by name.
"""
from __future__ import annotations

import json
import unittest

from app.agents.adzump.creative_intelligence.models import (
    Creative, Essence, Rendition,
)
from app.agents.adzump.stores import competitors


class CompetitorKeyTests(unittest.TestCase):
    def test_identity_keys(self):
        # Identity = canonical website, path included: project pages on one
        # developer site are separate competitors (Kailash 2026-09-23). The URL
        # canonical form itself is tested with normalize_business_url.
        long_path = "https://x.com/" + "a" * 600
        for raw, expected in [
            ("https://www.Nike.com/air", "https://nike.com/air"),
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
