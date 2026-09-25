"""Unit: stores/competitors.py - competitor identity keys + row mapping.

A competitor's key is its canonical website (host + path), clamped to the url
column so the key a read looks up is exactly what both writers store; a
website-less competitor keys by name.
"""
from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
