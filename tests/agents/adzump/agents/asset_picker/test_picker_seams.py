"""Characterization of the two deterministic scrape seams the slice-1 golden
does NOT cover: the message builder and the JSON parser. Together with
test_resolve_picks, this freezes the ENTIRE deterministic chain around the
model (build → [model] → parse → resolve) so the slice-2 VisionJudge refactor
can't change scrape's behavior without a test going red.

Below the model, no mocks: real functions, hand-built inputs.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest \
        tests.agents.adzump.agents.asset_picker.test_picker_seams -v
"""

from __future__ import annotations

import base64
import unittest

from app.agents.adzump.agents.asset_picker.agent import (
    _build_user_message_and_images, _parse_selection,
)
from app.agents.adzump.agents.product.models import SiteImage


def _img(src: str, source: str = "img") -> SiteImage:
    return SiteImage(src=src, source=source)


class BuildMessageTests(unittest.TestCase):
    """_build_user_message_and_images: candidates + thumbnails → (text, image blocks)."""

    def _cands_and_fetched(self):
        cands = [
            _img("https://x.com/logo.png", "jsonld"),   # 0 — has thumbnail
            _img("https://x.com/icon.svg", "img"),       # 1 — SVG, NO thumbnail
            _img("https://x.com/hero.webp", "img"),      # 2 — has thumbnail
        ]
        fetched = {
            "https://x.com/logo.png":  {"thumb_bytes": b"AAAA", "thumb_content_type": "image/png"},
            "https://x.com/hero.webp": {"thumb_bytes": b"BBBB"},   # no content_type → default
        }
        return cands, fetched

    def test_blocks_with_screenshot(self):
        cands, fetched = self._cands_and_fetched()
        text, blocks = _build_user_message_and_images(
            cands, fetched, summary="A real estate site", meta_json="[meta]",
            full_page_screenshot_b64="SHOT64",
        )
        # screenshot block #0, then one block per candidate WITH a thumbnail (0 and 2).
        self.assertEqual(len(blocks), 3)
        self.assertEqual(blocks[0]["source"]["data"], "SHOT64")
        self.assertEqual(blocks[0]["source"]["media_type"], "image/jpeg")
        self.assertEqual(blocks[1]["source"]["data"], base64.b64encode(b"AAAA").decode())
        self.assertEqual(blocks[1]["source"]["media_type"], "image/png")
        self.assertEqual(blocks[2]["source"]["data"], base64.b64encode(b"BBBB").decode())
        self.assertEqual(blocks[2]["source"]["media_type"], "image/jpeg")   # default
        # text: every candidate described in index order; the SVG is text-only.
        self.assertIn("[Candidate 0]", text)
        self.assertIn("[Candidate 1]", text)
        self.assertIn("[Candidate 2]", text)
        self.assertIn("no thumbnail", text)              # the SVG entry
        self.assertLess(text.index("[Candidate 0]"), text.index("[Candidate 2]"))  # order

    def test_no_screenshot_falls_back(self):
        cands, fetched = self._cands_and_fetched()
        text, blocks = _build_user_message_and_images(
            cands, fetched, summary="s", meta_json="[meta]", full_page_screenshot_b64=None,
        )
        # no screenshot block → only the 2 thumbnailed candidates.
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["source"]["data"], base64.b64encode(b"AAAA").decode())
        self.assertIn("Candidates (3 total", text)       # the v7 fallback header


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
