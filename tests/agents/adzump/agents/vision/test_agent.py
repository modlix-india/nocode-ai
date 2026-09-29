"""vision/agent.py below the model: the deterministic seams around the vision
call (build message -> [model] -> parse -> resolve), with hand-built inputs and
no mocks. Judgment quality (did it call #0 a logo?) is the manual eval, never a
unit test.

Fixture = a real scrape of **Purva Sparkling Springs** (purvasparklingspring.com,
our canonical real-estate test product): developer + project logos, a lakefront
elevation hero, a clubhouse amenity, a 3BHK floor plan and a RERA disclaimer
banner (the quintessential "unused" creative). Raster-only: html_parser drops
every SVG (v9), so an .svg never reaches _resolve_picks.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest \
        tests.agents.adzump.agents.vision.test_agent -v
"""

from __future__ import annotations

import base64
import unittest

from app.agents.adzump.agents.product.models import SiteImage
from app.agents.adzump.agents.vision.agent import (
    _build_review_message,
    _build_user_message_and_images,
    _filename_suggests_logo,
    _parse_review,
    _parse_selection,
    _resolve_picks,
)
from app.agents.adzump.agents.vision.models import (
    AssetSelection, CreativeChoice, LogoChoice,
)

_SITE = "https://purvasparklingspring.com/img"
DEV_LOGO = f"{_SITE}/puravankara-logo.png"          # developer (parent) logo
PROJ_LOGO = f"{_SITE}/sparkling-springs-logo.webp"  # project logo
PROJECT_SVG = f"{_SITE}/sparkling-springs.svg"      # SVG, no thumbnail
HERO = f"{_SITE}/lakefront-elevation.webp"          # hero render
AMENITY = f"{_SITE}/clubhouse-infinity-pool.jpg"    # amenity
FLOOR_PLAN = f"{_SITE}/3bhk-villa-floor-plan.png"   # floor plan
RERA_BANNER = f"{_SITE}/rera-disclaimer-banner.jpg" # unused (RERA junk)
LOGO_THUMB, HERO_THUMB = b"LOGO_PNG", b"HERO_JPG"


def _img(src: str, source: str = "img") -> SiteImage:
    return SiteImage(src=src, source=source)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


# ── review-each: N images in, one verdict each ─────────────────────────────
class ReviewTests(unittest.TestCase):
    def test_parses_one_verdict_per_image(self):
        text = (
            "```json\n"
            '{"verdicts": ['
            '{"idx":0,"role":"logo","relevant":true,"confidence":0.95,'
            '"needs_user":false,"question":"","reasoning":"wordmark"},'
            '{"idx":1,"role":"unknown","relevant":true,"confidence":0.4,'
            '"needs_user":true,"question":"floor plan or site map?","reasoning":"ambiguous"}'
            "]}\n```"
        )
        res = _parse_review(text)
        self.assertEqual([v.idx for v in res.verdicts], [0, 1])
        self.assertEqual(res.verdicts[0].role, "logo")
        # the ambiguous one flags the user instead of guessing
        self.assertTrue(res.verdicts[1].needs_user)
        self.assertEqual(res.verdicts[1].question, "floor plan or site map?")
        self.assertEqual(_parse_review("the model said nothing useful").verdicts, [])

    def test_one_block_per_image_in_order(self):
        images = [
            {"data": b"AAAA", "content_type": "image/png"},
            {"data": b"BBBB"},                                  # no content_type -> default
        ]
        text, blocks = _build_review_message(images, summary="a product")
        self.assertEqual(
            [(b["source"]["data"], b["source"]["media_type"]) for b in blocks],
            [(_b64(b"AAAA"), "image/png"), (_b64(b"BBBB"), "image/jpeg")])
        self.assertIn("2 image(s)", text)


# ── select: candidates + thumbnails -> message; model text -> selection ─────
def _scrape():
    # header logo (thumbnailed), an SVG icon (no thumbnail: a text-only entry)
    # and a hero whose thumb has no content-type (image/jpeg default)
    cands = [_img(DEV_LOGO, "jsonld"), _img(PROJECT_SVG), _img(HERO)]
    fetched = {
        DEV_LOGO: {"thumb_bytes": LOGO_THUMB, "thumb_content_type": "image/png"},
        HERO: {"thumb_bytes": HERO_THUMB},
    }
    return cands, fetched


class BuildMessageTests(unittest.TestCase):
    def test_blocks_with_and_without_a_screenshot(self):
        thumbs = [(_b64(LOGO_THUMB), "image/png"), (_b64(HERO_THUMB), "image/jpeg")]
        for label, shot, expected, header in [
            ("screenshot first, then one block per thumbnailed candidate", "SHOT64",
             [("SHOT64", "image/jpeg")] + thumbs, None),
            ("no screenshot: only the thumbnails, fallback header", None, thumbs,
             "Candidates (3 total"),
        ]:
            with self.subTest(label):
                text, blocks = _build_user_message_and_images(
                    *_scrape(), summary="Lakeside 3BHK villas", meta_json="[meta]",
                    full_page_screenshot_b64=shot)
                self.assertEqual(
                    [(b["source"]["data"], b["source"]["media_type"]) for b in blocks],
                    expected)
                if header:
                    self.assertIn(header, text)

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


class ParseSelectionTests(unittest.TestCase):
    def test_rows(self):
        for label, text, logos, confidence in [
            ("fenced json",
             "Here are my picks:\n```json\n"
             '{"logos": [{"idx": 0, "role": "developer"}], '
             '"creatives": [{"idx": 2, "role": "hero"}], "confidence": 0.8}\n```',
             [0], 0.8),
            ("bare json", '{"logos": [{"idx": 1}], "confidence": 0.5}', [1], 0.5),
            ("garbage: empty", "the model declined to answer", [], 0.0),
            # logos must be a list of objects; a string fails validation
            ("invalid schema: empty", '{"logos": "not-a-list", "confidence": 0.9}', [], 0.0),
        ]:
            with self.subTest(label):
                sel = _parse_selection(text)
                self.assertEqual([logo.idx for logo in sel.logos], logos)
                self.assertEqual(sel.confidence, confidence)


# ── resolve: the model's picks -> ProductAssets ────────────────────────────
CANDS = [
    _img(DEV_LOGO, "jsonld"),    # 0 - developer logo (Organization.logo)
    _img(PROJ_LOGO, "og"),       # 1 - project logo (og:image)
    _img(HERO),                  # 2 - hero
    _img(AMENITY),               # 3 - amenity
    _img(FLOOR_PLAN),            # 4 - floor plan
    _img(RERA_BANNER),           # 5 - unused
    _img(DEV_LOGO),              # 6 - DUP url of idx 0
]


class ResolvePicksGoldenTests(unittest.TestCase):

    def test_resolve_picks_golden(self):
        sel = AssetSelection(
            logos=[
                LogoChoice(idx=0, role="developer", background_hint="dark"),
                LogoChoice(idx=1, role="project", background_hint="light"),
                LogoChoice(idx=6, role="cobrand"),     # dup url of 0 -> deduped away
            ],
            creatives=[
                CreativeChoice(idx=2, role="hero"),
                CreativeChoice(idx=3, role="amenity"),
                CreativeChoice(idx=4, role="floor_plan"),
                CreativeChoice(idx=5, role="unused"),
            ],
            confidence=0.9,
        )
        out = _resolve_picks(sel, CANDS)

        # Logos: dup-url idx 6 dropped -> 2 kept, with derived format + background.
        self.assertEqual([logo.url for logo in out.logos], [DEV_LOGO, PROJ_LOGO])
        self.assertEqual([logo.role for logo in out.logos], ["developer", "project"])
        self.assertEqual([logo.format for logo in out.logos], ["png", "webp"])
        self.assertEqual([logo.background for logo in out.logos], ["dark", "light"])
        self.assertEqual([logo.source for logo in out.logos], ["jsonld", "og"])

        # creative_image_urls excludes the 'unused' RERA banner.
        self.assertEqual(out.creative_image_urls, [HERO, AMENITY, FLOOR_PLAN])
        # creatives_with_role keeps ALL four (incl. unused), in order.
        self.assertEqual([c.role for c in out.creatives_with_role],
                         ["hero", "amenity", "floor_plan", "unused"])

        # Completeness derived by code: hero + >=1 amenity + floor_plan -> complete.
        self.assertEqual(out.creative_completeness.verdict, "complete")
        self.assertTrue(out.creative_completeness.hero_found)
        self.assertEqual(out.creative_completeness.amenities_count, 1)
        self.assertTrue(out.creative_completeness.floor_plan_found)
        self.assertEqual(out.creative_completeness.missing_categories, [])

        self.assertEqual(out.confidence, 0.9)

    def test_logo_named_creative_dropped_by_safety_net(self):
        """PR #91 J3: the filename guard is wired INSIDE _resolve_picks - a
        sub-brand wordmark the vision pass let through (clublogo.png) never
        reaches the creative bucket."""
        cands = CANDS + [_img(f"{_SITE}/clublogo.png")]  # idx 7
        sel = AssetSelection(
            creatives=[CreativeChoice(idx=2, role="hero"),
                       CreativeChoice(idx=7, role="amenity")],
        )
        out = _resolve_picks(sel, cands)
        self.assertEqual(out.creative_image_urls, [HERO])
        self.assertEqual([c.role for c in out.creatives_with_role], ["hero"])
        # v9 I-8: floor_plan is tracked but never a missing category -
        # 'complete' only needs hero + amenity.
        self.assertEqual(out.creative_completeness.missing_categories, ["amenity"])

    def test_resolve_picks_drops_out_of_range_indices(self):
        # OOB indices must be silently dropped, not crash.
        sel = AssetSelection(
            logos=[LogoChoice(idx=0, role="main"), LogoChoice(idx=99)],
            creatives=[CreativeChoice(idx=2, role="hero"),
                       CreativeChoice(idx=50, role="amenity")],
            confidence=0.5,
        )
        out = _resolve_picks(sel, CANDS)
        self.assertEqual([logo.url for logo in out.logos], [DEV_LOGO])
        self.assertEqual(out.creative_image_urls, [HERO])
        self.assertEqual([c.role for c in out.creatives_with_role], ["hero"])


class FilenameSuggestsLogoTests(unittest.TestCase):
    """The creative-bucket guard: filename token says wordmark (clublogo.png
    etc.). Path tokens don't count - only the filename itself."""

    def test_logo_substring_matches(self):
        variants = [
            ("https://x.com/clublogo.png", True),
            ("https://x.com/CLUBLOGO.png", True),          # case-insensitive
            ("https://x.com/club_logo.png", True),
            ("https://x.com/brand-logo-white.svg", True),
            ("https://x.com/logos/main.png", False),       # 'logos' in PATH only
            ("https://x.com/wordmark_dark.svg", True),
            ("https://x.com/products/villa-1.jpg", False),  # clearly a product
        ]
        for url, expected in variants:
            with self.subTest(url):
                self.assertEqual(_filename_suggests_logo(url), expected)


if __name__ == "__main__":
    unittest.main()
