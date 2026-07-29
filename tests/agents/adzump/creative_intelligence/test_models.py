"""Unit: creative_intelligence/models.py - the one typed shape.

Locks: vendor-names -> stored camelCase (by_alias) -> back round-trip, counts
computed from the creatives list, Essence enum enforcement + escape defaults,
and the deterministic winner_signal longevity thresholds.
"""
from __future__ import annotations

import unittest

import pydantic

from app.agents.adzump.creative_intelligence.models import Creative, Competitor, Essence


class ModelShapeTests(unittest.TestCase):
    def test_shape_counts_essence_and_winner_signal(self):
        with self.subTest("creative dumps the stored camelCase shape"):
            d = Creative(creative_id="a1", media_type="video",
                         source_asset_url="v.mp4",
                         poster_source_url="p.jpg").model_dump(by_alias=True)
            for camel in ("creativeId", "mediaType", "sourceAssetUrl", "posterSourceUrl",
                          "fileUrl", "posterUrl", "isActive", "publisherPlatforms",
                          "winnerSignal"):
                self.assertIn(camel, d)
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
        with self.subTest("essence: camelCase round-trip, enums enforced, escape defaults"):
            e = Essence(hook_type="social_proof", awareness_stage="problem_aware",
                        ocr_text="50% OFF")
            d = e.model_dump(by_alias=True)
            for camel in ("hookType", "awarenessStage", "ocrText"):
                self.assertIn(camel, d)
            self.assertEqual(Essence.model_validate(d).awareness_stage, "problem_aware")
            with self.assertRaises(pydantic.ValidationError):
                Essence(hook_type="not_a_real_hook")
            e0 = Essence()
            self.assertEqual((e0.hook_type, e0.awareness_stage, e0.offer, e0.media_format),
                             ("other", "unknown", "none", "other"))
            back = Creative.model_validate(
                Creative(creative_id="c",
                         essence=Essence(hook_type="urgency")).model_dump(by_alias=True))
            self.assertEqual(back.essence.hook_type, "urgency")
        for days, expected in [(120, "evergreen"), (90, "evergreen"), (60, "winner"),
                               (30, "promising"), (0, "testing")]:
            with self.subTest(days_running=days):
                self.assertEqual(
                    Creative(creative_id="x", days_running=days).winner_signal, expected)
        with self.subTest("winner_signal serializes by alias"):
            self.assertEqual(
                Creative(creative_id="x",
                         days_running=95).model_dump(by_alias=True)["winnerSignal"],
                "evergreen")


if __name__ == "__main__":
    unittest.main()
