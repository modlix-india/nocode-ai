"""Unit: creative_intelligence/models.py - the one typed shape.

Locks: counts are computed from the creatives list, and a stored camelCase
record revalidates without a migration. Essence enum coercion is locked in
test_essence.py through the parse path.
"""
from __future__ import annotations

import unittest

from app.agents.adzump.creative_intelligence.models import Creative, Competitor


class ModelShapeTests(unittest.TestCase):
    def test_counts_and_stored_record(self):
        with self.subTest("counts are computed from creatives, can't drift"):
            d = Competitor(competitor_key="nike.com", creatives=[
                Creative(creative_id="1", is_active=True),
                Creative(creative_id="2"),
                Creative(creative_id="3", is_active=True),
            ]).model_dump(by_alias=True)
            self.assertEqual((d["totalCreatives"], d["activeCreatives"]), (3, 2))
        with self.subTest("stored record revalidates with no migration"):
            comp = Competitor.model_validate({
                "competitorKey": "nike.com", "name": "Nike",
                "creatives": [{"creativeId": "1", "mediaType": "image",
                               "sourceAssetUrl": "i.jpg", "isActive": True}],
                "lastFetchedAt": "2026-07-01T00:00:00+00:00", "fetchStatus": "ok",
            })
            self.assertEqual((comp.total_creatives, comp.active_creatives), (1, 1))


if __name__ == "__main__":
    unittest.main()
