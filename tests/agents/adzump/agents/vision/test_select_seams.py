"""Characterization of the two deterministic scrape seams the slice-1 golden
does NOT cover: the message builder and the JSON parser. Together with
test_resolve_picks, this freezes the ENTIRE deterministic chain around the
model (build → [model] → parse → resolve) so the slice-2 VisionAnalyst refactor
can't change scrape's behavior without a test going red.

Below the model, no mocks: real functions, hand-built inputs.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest \
        tests.agents.adzump.agents.vision.test_select_seams -v
"""

from __future__ import annotations

import base64
import unittest

from app.agents.adzump.agents.vision.agent import (
    _build_user_message_and_images, _parse_selection,
)
from app.agents.adzump.agents.product.models import SiteImage


def _img(src: str, source: str = "img") -> SiteImage:
    return SiteImage(src=src, source=source)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


# A Purva Sparkling Springs scrape: header logo (thumbnailed), an SVG icon
# (no thumbnail → text-only entry, the case under test), and a hero render
# (thumbnailed, but its thumb has no content-type → image/jpeg default).
_SITE = "https://purvasparklingspring.com/img"
DEV_LOGO = f"{_SITE}/puravankara-logo.png"       # 0 — thumbnailed
PROJECT_SVG = f"{_SITE}/sparkling-springs.svg"   # 1 — SVG, no thumbnail
HERO = f"{_SITE}/lakefront-elevation.webp"       # 2 — thumbnailed
LOGO_THUMB, HERO_THUMB = b"LOGO_PNG", b"HERO_JPG"


def _scrape():
    cands = [_img(DEV_LOGO, "jsonld"), _img(PROJECT_SVG), _img(HERO)]
    fetched = {
        DEV_LOGO: {"thumb_bytes": LOGO_THUMB, "thumb_content_type": "image/png"},
        HERO: {"thumb_bytes": HERO_THUMB},   # no content_type → image/jpeg default
    }
    return cands, fetched


class BuildMessageTests(unittest.TestCase):
    """_build_user_message_and_images: candidates + thumbnails → (text, image blocks)."""

    def test_screenshot_first_then_one_block_per_thumbnailed_candidate(self):
        _, blocks = _build_user_message_and_images(
            *_scrape(), summary="Lakeside 3BHK villas", meta_json="[meta]",
            full_page_screenshot_b64="SHOT64",
        )
        # block 0 = screenshot; then logo + hero (the SVG has no thumb → no block).
        self.assertEqual(
            [(b["source"]["data"], b["source"]["media_type"]) for b in blocks],
            [("SHOT64", "image/jpeg"),
             (_b64(LOGO_THUMB), "image/png"),
             (_b64(HERO_THUMB), "image/jpeg")],
        )

    def test_every_candidate_described_in_order_svg_text_only(self):
        text, _ = _build_user_message_and_images(
            *_scrape(), summary="Lakeside 3BHK villas", meta_json="[meta]",
            full_page_screenshot_b64="SHOT64",
        )
        for i in (0, 1, 2):
            with self.subTest(candidate=i):
                self.assertIn(f"[Candidate {i}]", text)
        self.assertIn("no thumbnail", text)   # the SVG entry is text-only
        self.assertLess(text.index("[Candidate 0]"), text.index("[Candidate 2]"))

    def test_no_screenshot_omits_block_and_uses_fallback_header(self):
        text, blocks = _build_user_message_and_images(
            *_scrape(), summary="Lakeside 3BHK villas", meta_json="[meta]",
            full_page_screenshot_b64=None,
        )
        # no screenshot → only the 2 thumbnailed candidates; v7 fallback header.
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["source"]["data"], _b64(LOGO_THUMB))
        self.assertIn("Candidates (3 total", text)


class ParseSelectionTests(unittest.TestCase):
    """_parse_selection: model's final text → AssetSelection (empty on any failure)."""

    def test_fenced_json_parses(self):
        text = (
            "Here are my picks:\n```json\n"
            '{"logos": [{"idx": 0, "role": "developer"}], '
            '"creatives": [{"idx": 2, "role": "hero"}], "confidence": 0.8}\n```'
        )
        sel = _parse_selection(text)
        self.assertEqual([l.idx for l in sel.logos], [0])
        self.assertEqual([c.role for c in sel.creatives], ["hero"])
        self.assertEqual(sel.confidence, 0.8)

    def test_bare_json_parses(self):
        sel = _parse_selection('{"logos": [{"idx": 1}], "confidence": 0.5}')
        self.assertEqual([l.idx for l in sel.logos], [1])
        self.assertEqual(sel.confidence, 0.5)

    def test_garbage_returns_empty(self):
        sel = _parse_selection("the model declined to answer")
        self.assertEqual(sel.logos, [])
        self.assertEqual(sel.confidence, 0.0)

    def test_invalid_schema_returns_empty(self):
        # logos must be a list of objects; a string fails validation → empty.
        sel = _parse_selection('{"logos": "not-a-list", "confidence": 0.9}')
        self.assertEqual(sel.logos, [])
        self.assertEqual(sel.confidence, 0.0)


if __name__ == "__main__":
    unittest.main()
