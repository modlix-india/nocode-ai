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
        with self.subTest("fetched but library had none -> explicit 'No ads found'"):
            # absence must not masquerade as "runs no ads": a FETCHED-empty
            # competitor says so; an unfetched one (above) claims nothing.
            blocks = []
            empty = {**rival, "creatives": [], "totalCreatives": 0, "activeCreatives": 0}
            render_competitors(blocks, {"competitors": [empty]})
            card = [b for b in blocks if b["type"] == "collapsible"][0]
            self.assertEqual(card["badge"], "No ads found")
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
        with self.subTest("video click-through is the playable rehosted video"):
            children = []
            render_competitor_creatives(
                children,
                [{"mediaType": "video", "posterUrl": "p.jpg", "fileUrl": "v.mp4"}], 1, 1)
            card = _carousel_cards(children)[0]
            self.assertEqual((card["url"], card["thumb_url"]), ("v.mp4", "p.jpg"))
        with self.subTest("badges + non-zero metrics line"):
            children = []
            render_competitor_creatives(children, [{
                "mediaType": "image", "fileUrl": "f.jpg", "isActive": True,
                "daysRunning": 112,
                "metrics": {"impressions": 2_500_000, "likes": 0, "views": 980},
            }], 1, 1)
            card = _carousel_cards(children)[0]
            self.assertEqual([b["label"] for b in card["badges"]], ["Active", "112d"])
            self.assertEqual(card["badges"][0]["tone"], "active")
            self.assertEqual(card["meta"], "2.5M impressions · 980 views")
        with self.subTest("string/suffixed vendor metrics render, never crash the panel"):
            # regression: the ad library sends counts as '10K'/'1,234'/ranges;
            # int() on those threw and rerender_craft swallowed it -> creatives
            # silently vanished from the panel (B2). Now coerced, never raised.
            children = []
            render_competitor_creatives(children, [{
                "mediaType": "image", "fileUrl": "f.jpg", "daysRunning": "112",
                "metrics": {"impressions": "10K", "likes": "1,234", "views": "1K-5K"},
            }], 1, 0)
            card = _carousel_cards(children)[0]
            self.assertEqual(card["meta"], "10K impressions · 1K views · 1.2K likes")
            self.assertIn("112d", [b["label"] for b in card["badges"]])
        with self.subTest("paused + zero metrics -> paused badge, no meta key"):
            children = []
            render_competitor_creatives(children, [_img(1)], 1, 0)
            card = _carousel_cards(children)[0]
            self.assertEqual(card["badges"], [{"label": "Paused", "tone": "paused"}])
            self.assertNotIn("meta", card)
        with self.subTest("paused with lastSeen carries the recency chip"):
            from datetime import datetime, timedelta, timezone
            seen = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
            children = []
            render_competitor_creatives(children, [
                {"mediaType": "image", "fileUrl": "f.jpg", "lastSeen": seen}], 1, 0)
            labels = [b["label"] for b in _carousel_cards(children)[0]["badges"]]
            self.assertEqual(labels, ["Paused", "seen 45d ago"])
            # active ads don't need it; garbage timestamps stay silent
            children = []
            render_competitor_creatives(children, [
                {"mediaType": "image", "fileUrl": "f.jpg", "isActive": True,
                 "lastSeen": seen}], 1, 1)
            self.assertEqual([b["label"] for b in _carousel_cards(children)[0]["badges"]],
                             ["Active"])
            children = []
            render_competitor_creatives(children, [
                {"mediaType": "image", "fileUrl": "f.jpg", "lastSeen": "junk"}], 1, 0)
            self.assertEqual([b["label"] for b in _carousel_cards(children)[0]["badges"]],
                             ["Paused"])
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
