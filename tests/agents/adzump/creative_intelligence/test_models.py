"""Unit: creative_intelligence/models.py - the one shape.

Locks the invariant the typed model exists to guarantee: a Creative/Competitor
round-trips vendor-names -> stored camelCase (by_alias) -> back, with the counts
computed from the creatives list so they can never drift, and existing stored
records validate with no migration.
"""
from __future__ import annotations

import unittest

from app.agents.adzump.creative_intelligence.models import Creative, Competitor


class ModelRoundTripTests(unittest.TestCase):
    def test_dump_by_alias_is_the_stored_camelcase_shape(self):
        c = Creative(creative_id="a1", media_type="video",
                     source_asset_url="v.mp4", poster_source_url="p.jpg", headline="Hi")
        d = c.model_dump(by_alias=True)
        for camel in ("creativeId", "mediaType", "sourceAssetUrl", "posterSourceUrl",
                      "fileUrl", "posterUrl", "isActive", "publisherPlatforms"):
            self.assertIn(camel, d)
        self.assertEqual(d["creativeId"], "a1")
        self.assertEqual(d["mediaType"], "video")

    def test_counts_are_computed_from_creatives(self):
        comp = Competitor(competitor_key="nike.com", creatives=[
            Creative(creative_id="1", is_active=True),
            Creative(creative_id="2", is_active=False),
            Creative(creative_id="3", is_active=True),
        ])
        d = comp.model_dump(by_alias=True)
        self.assertEqual(d["totalCreatives"], 3)
        self.assertEqual(d["activeCreatives"], 2)

    def test_stored_record_revalidates_with_no_migration(self):
        stored = {
            "competitorKey": "nike.com", "name": "Nike",
            "creatives": [{"creativeId": "1", "mediaType": "image",
                           "sourceAssetUrl": "i.jpg", "isActive": True}],
            "lastFetchedAt": "2026-07-01T00:00:00+00:00", "fetchStatus": "ok",
        }
        comp = Competitor.model_validate(stored)
        self.assertEqual(comp.competitor_key, "nike.com")
        self.assertEqual(comp.total_creatives, 1)
        self.assertEqual(comp.active_creatives, 1)
        self.assertEqual(comp.creatives[0].source_asset_url, "i.jpg")


if __name__ == "__main__":
    unittest.main()
