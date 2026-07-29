"""Unit: tools/craft.py - competitor cards + nested creatives.

One collapsible per rival (NO comparison table): the header carries the
clickable website link + "N ads" badge so a CLOSED card still shows both;
the body holds detail key-values and, below them, the creatives as metric
tiles + a horizontal carousel. One builder behind every panel render.
"""
from __future__ import annotations

import unittest

from app.agents.adzump.tools.craft import (
    _RENDER_PER_COMPETITOR,
    render_competitor_creatives,
    render_competitors,
)


def _img(i):
    return {"mediaType": "image", "fileUrl": f"img{i}.jpg", "headline": f"h{i}"}


def _carousel_cards(children):
    for block in children:
        if block["type"] == "carousel":
            return block["children"]
    return []


class CompetitorCardsTests(unittest.TestCase):
    def test_cards_and_nested_creatives(self):
        rival = {
            "name": "Nike", "url": "https://nike.com", "location": "Bengaluru",
            "weakness": "no villas", "key_usps": ["big", "fast"],
            "creatives": [_img(1)], "totalCreatives": 4, "activeCreatives": 3,
        }
        with self.subTest("no table; header link + ads badge; creatives nested in card"):
            blocks: list = []
            render_competitors(blocks, {"competitors": [rival]})
            self.assertFalse(any(b["type"] == "table" for b in blocks))
            cards = [b for b in blocks if b["type"] == "collapsible"]
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0]["summary"], "Nike")
            self.assertEqual(cards[0]["summary_url"], "https://nike.com")
            self.assertEqual(cards[0]["badge"], "4 ads")
            children = cards[0]["children"]
            kv = children[0]["items"]
            self.assertIn(("Gap", "no villas"),
                          [(i["key"], i["value"]) for i in kv])
            metric_rows = [b for b in children if b["type"] == "row"
                           and b["children"][0].get("type") == "metric"]
            labels = [m["label"] for m in metric_rows[0]["children"]]
            self.assertEqual(labels, ["Total ads", "Active", "Paused"])
            self.assertEqual(len(_carousel_cards(children)), 1)
        with self.subTest("no creatives fetched yet -> no badge, no carousel"):
            blocks = []
            bare = {k: v for k, v in rival.items()
                    if k not in ("creatives", "totalCreatives", "activeCreatives")}
            render_competitors(blocks, {"competitors": [bare]})
            card = [b for b in blocks if b["type"] == "collapsible"][0]
            self.assertNotIn("badge", card)
            self.assertFalse(_carousel_cards(card["children"]))
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
                children: list = []
                render_competitor_creatives(children, [creative], 1, 1)
                self.assertEqual(_carousel_cards(children)[0]["url"], expected)
        with self.subTest("video gets the play marker from its poster"):
            children = []
            render_competitor_creatives(
                children,
                [{"mediaType": "video", "posterUrl": "p.jpg", "headline": "Watch"}], 1, 1)
            self.assertTrue(_carousel_cards(children)[0]["caption"].startswith("▶"))
        with self.subTest("payload cap; no-usable-image is a noop"):
            children = []
            render_competitor_creatives(children, [_img(i) for i in range(20)], 20, 4)
            self.assertEqual(len(_carousel_cards(children)), _RENDER_PER_COMPETITOR)
            children = []
            render_competitor_creatives(
                children, [{"mediaType": "image", "headline": "x"}], 5, 2)
            self.assertEqual(children, [])


if __name__ == "__main__":
    unittest.main()
