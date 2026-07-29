"""Unit: tools/craft.py - render_competitor_creatives.

The shared builder used by both the on-demand append and the full-panel rebuild,
so a rebuild redraws creatives instead of dropping them. Locks: skip creatives
with no usable image, rehosted-over-vendor URL precedence, video poster + play
marker, the thumbnail cap, and the 2-up row pairing.
"""
from __future__ import annotations

import unittest

from app.agents.adzump.tools.craft import render_competitor_creatives, _RENDER_PER_COMPETITOR


def _img(i):
    return {"mediaType": "image", "fileUrl": f"img{i}.jpg", "headline": f"h{i}"}


def _rows(blocks):
    return [b for b in blocks if b["type"] == "row"]


def _image_cards(blocks):
    cards = []
    for r in _rows(blocks):
        cards += [ch for ch in r["children"] if ch.get("type") == "image"]
    return cards


class RenderCompetitorCreativesTests(unittest.TestCase):
    def test_render(self):
        for name, creative_list in [
            ("empty list", []),
            ("no usable image", [{"mediaType": "image", "headline": "x"}]),
        ]:
            with self.subTest(noop=name):
                blocks = []
                render_competitor_creatives(blocks, "Nike", creative_list, 5, 2)
                self.assertEqual(blocks, [])
        with self.subTest("heading + metric row present"):
            blocks = []
            render_competitor_creatives(blocks, "Nike", [_img(1)], 4, 3)
            self.assertTrue(any("Nike" in b["text"] for b in blocks if b["type"] == "heading"))
            metric_rows = [r for r in _rows(blocks)
                           if r["children"] and r["children"][0].get("type") == "metric"]
            labels = [m["label"] for m in metric_rows[0]["children"]]
            self.assertIn("Total ads", labels)
            self.assertIn("Paused", labels)  # present because total > 0
        for name, creative, expected in [
            ("image rehosted", {"mediaType": "image", "fileUrl": "f.jpg",
                                "sourceAssetUrl": "v.jpg"}, "f.jpg"),
            ("carousel poster rehosted", {"mediaType": "carousel", "posterUrl": "p.jpg",
                                          "sourceAssetUrl": "v.jpg"}, "p.jpg"),
            ("vendor fallback", {"mediaType": "carousel", "sourceAssetUrl": "v.jpg"}, "v.jpg"),
            ("video poster", {"mediaType": "video", "posterUrl": "p.jpg",
                              "posterSourceUrl": "vp.jpg"}, "p.jpg"),
        ]:
            with self.subTest(url_precedence=name):
                blocks = []
                render_competitor_creatives(blocks, "Nike", [creative], 1, 1)
                self.assertEqual(_image_cards(blocks)[0]["url"], expected)
        with self.subTest("video gets the play marker from its poster"):
            blocks = []
            render_competitor_creatives(
                blocks, "Nike",
                [{"mediaType": "video", "posterUrl": "p.jpg", "headline": "Watch"}], 1, 1)
            self.assertTrue(_image_cards(blocks)[0]["caption"].startswith("▶"))
        with self.subTest("caps thumbnails and pairs rows 2-up"):
            blocks = []
            render_competitor_creatives(blocks, "Nike", [_img(i) for i in range(10)], 10, 4)
            self.assertEqual(len(_image_cards(blocks)), _RENDER_PER_COMPETITOR)  # 6
            image_rows = [r for r in _rows(blocks)
                          if r["children"] and r["children"][0].get("type") == "image"]
            self.assertEqual(len(image_rows), 3)
            for r in image_rows:
                self.assertLessEqual(len(r["children"]), 2)


if __name__ == "__main__":
    unittest.main()
