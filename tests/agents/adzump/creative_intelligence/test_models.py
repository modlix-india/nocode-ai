"""Unit: creative_intelligence/models.py - the one shape.

Locks the invariant the typed model exists to guarantee: a Creative/Competitor
round-trips vendor-names -> stored camelCase (by_alias) -> back, with the counts
computed from the creatives list so they can never drift, and existing stored
records validate with no migration.
"""
from __future__ import annotations

import unittest

import pydantic

from app.agents.adzump.creative_intelligence.models import Creative, Competitor, Essence


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


class EssenceTests(unittest.TestCase):
    def test_all_multiword_fields_serialize_camelcase(self):
        e = Essence(hook_type="social_proof", hook_text="Tired?", awareness_stage="problem_aware",
                    copy_framework="PAS", emotional_angle="relief", media_format="ugc",
                    visual_style="ugc_authentic", ocr_text="50% OFF")
        d = e.model_dump(by_alias=True)
        for camel in ("hookType", "hookText", "awarenessStage", "copyFramework",
                      "emotionalAngle", "mediaFormat", "visualStyle", "ocrText"):
            self.assertIn(camel, d)
        self.assertEqual(Essence.model_validate(d).awareness_stage, "problem_aware")

    def test_enum_values_are_enforced(self):
        with self.assertRaises(pydantic.ValidationError):
            Essence(hook_type="not_a_real_hook")

    def test_defaults_are_the_escape_values(self):
        e = Essence()
        self.assertEqual((e.hook_type, e.awareness_stage, e.offer, e.media_format),
                         ("other", "unknown", "none", "other"))

    def test_essence_attaches_and_round_trips_on_creative(self):
        c = Creative(creative_id="c", essence=Essence(hook_type="urgency"))
        back = Creative.model_validate(c.model_dump(by_alias=True))
        self.assertEqual(back.essence.hook_type, "urgency")


class WinnerSignalTests(unittest.TestCase):
    def test_longevity_thresholds(self):
        def sig(days):
            return Creative(creative_id="x", days_running=days).winner_signal
        cases = [(120, "evergreen"), (90, "evergreen"), (75, "winner"), (60, "winner"),
                 (45, "promising"), (30, "promising"), (5, "testing"), (0, "testing")]
        for days, expected in cases:
            with self.subTest(days=days):
                self.assertEqual(sig(days), expected)

    def test_serializes_by_alias_and_is_not_a_model_call(self):
        d = Creative(creative_id="x", days_running=95).model_dump(by_alias=True)
        self.assertEqual(d["winnerSignal"], "evergreen")


if __name__ == "__main__":
    unittest.main()
