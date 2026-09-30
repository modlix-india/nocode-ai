"""Unit: creative_intelligence/models.py - the one typed shape.

Locks: counts are computed from the creatives list (tools/creatives.py reads
totalCreatives). The stored read path is locked by stores/test_competitors.py
(CreativeRoundTripTests); essence enum coercion by test_essence.py.
"""
from __future__ import annotations

import unittest

from app.agents.adzump.creative_intelligence.models import Creative, Competitor


class ModelShapeTests(unittest.TestCase):
    def test_counts_are_computed_from_creatives(self):
        d = Competitor(competitor_key="nike.com", creatives=[
            Creative(creative_id="1", is_active=True),
            Creative(creative_id="2"),
            Creative(creative_id="3", is_active=True),
        ]).model_dump(by_alias=True)
        self.assertEqual((d["totalCreatives"], d["activeCreatives"]), (3, 2))


if __name__ == "__main__":
    unittest.main()
