"""Unit: app/agents/adzump/tools/craft.py - render_competitor_creatives.

The shared builder used by both the on-demand append and the full-panel rebuild,
so a rebuild redraws creatives instead of dropping them. Locks: skip creatives
with no usable image, video poster + play marker, the 6-thumbnail cap, and the
2-up row pairing. No-op when nothing is renderable.
"""
from __future__ import annotations

import unittest

from app.agents.adzump.tools.craft import render_competitor_creatives, _RENDER_PER_COMPETITOR


def _img(i, **over):
    d = {"mediaType": "image", "fileUrl": f"img{i}.jpg", "headline": f"h{i}"}
    d.update(over)
    return d


def _rows(blocks):
    return [b for b in blocks if b["type"] == "row"]


def _image_cards(blocks):
    cards = []
    for r in _rows(blocks):
        cards += [ch for ch in r["children"] if ch.get("type") == "image"]
    return cards


class RenderCompetitorCreativesTests(unittest.TestCase):
    def test_noop_on_empty(self):
        blocks = []
        render_competitor_creatives(blocks, "Nike", [], 0, 0)
        self.assertEqual(blocks, [])

    def test_noop_when_no_usable_image(self):
        blocks = []
        render_competitor_creatives(blocks, "Nike", [{"mediaType": "image", "headline": "x"}], 5, 2)
        self.assertEqual(blocks, [])

    def test_heading_and_metric_row_present(self):
        blocks = []
        render_competitor_creatives(blocks, "Nike", [_img(1)], 4, 3)
        headings = [b for b in blocks if b["type"] == "heading"]
        self.assertTrue(any("Nike" in h["text"] for h in headings))
        metric_rows = [r for r in _rows(blocks) if r["children"] and r["children"][0].get("type") == "metric"]
        self.assertEqual(len(metric_rows), 1)
        labels = [m["label"] for m in metric_rows[0]["children"]]
        self.assertIn("Total ads", labels)
        self.assertIn("Paused", labels)  # present because total > 0

    def test_video_gets_play_marker_from_poster(self):
        blocks = []
        render_competitor_creatives(
            blocks, "Nike",
            [{"mediaType": "video", "posterUrl": "p.jpg", "headline": "Watch"}], 1, 1)
        cards = _image_cards(blocks)
        self.assertEqual(len(cards), 1)
        self.assertTrue(cards[0]["caption"].startswith("▶"))
        self.assertEqual(cards[0]["url"], "p.jpg")

    def test_caps_at_six_and_pairs_two_up(self):
        blocks = []
        render_competitor_creatives(blocks, "Nike", [_img(i) for i in range(10)], 10, 4)
        cards = _image_cards(blocks)
        self.assertEqual(len(cards), _RENDER_PER_COMPETITOR)  # 6
        image_rows = [r for r in _rows(blocks)
                      if r["children"] and r["children"][0].get("type") == "image"]
        # 6 cards, 2 per row -> 3 rows, each with <= 2
        self.assertEqual(len(image_rows), 3)
        for r in image_rows:
            self.assertLessEqual(len(r["children"]), 2)


if __name__ == "__main__":
    unittest.main()
