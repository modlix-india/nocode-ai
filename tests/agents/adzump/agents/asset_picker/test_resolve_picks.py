"""Characterization golden for _resolve_picks — the deterministic seam
BELOW the vision model (real logic, no mocks, no model call).

The picker's model returns an AssetSelection (indices + roles); _resolve_picks
maps that onto ProductAssets (urls + derived completeness). We hand-author the
selection (the model's hypothetical output) and lock the mapping, so design C's
"generalize the picker" step can't silently change scrape's picks.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest \
        tests.agents.adzump.agents.asset_picker.test_resolve_picks -v
"""

from __future__ import annotations

import unittest

from app.agents.adzump.agents.asset_picker.agent import _resolve_picks
from app.agents.adzump.agents.asset_picker.models import (
    AssetSelection, LogoChoice, CreativeChoice,
)
from app.agents.adzump.agents.product.models import SiteImage


def _img(src: str, source: str = "img") -> SiteImage:
    return SiteImage(src=src, source=source)


# A realistic candidate list: 2 logos (one a dup-url to exercise dedup),
# hero/amenity/floor_plan/unused creatives. Index order is the contract.
# Raster-only: html_parser drops every SVG at the parser (v9 — _is_svg_src,
# html_parser.py:159, "raster-only candidates by contract"), so an .svg can
# never reach _resolve_picks. Logos here are png/webp accordingly.
CANDS = [
    _img("https://x.com/dev-logo.png", "jsonld"),      # 0 — developer logo
    _img("https://x.com/project-logo.webp", "og"),     # 1 — project logo
    _img("https://x.com/hero.webp"),                   # 2 — hero
    _img("https://x.com/pool.jpg"),                    # 3 — amenity
    _img("https://x.com/floorplan.png"),               # 4 — floor plan
    _img("https://x.com/promo-banner.jpg"),            # 5 — unused
    _img("https://x.com/dev-logo.png"),                # 6 — DUP url of idx 0
]


class ResolvePicksGoldenTests(unittest.TestCase):

    def test_resolve_picks_golden(self):
        sel = AssetSelection(
            logos=[
                LogoChoice(idx=0, role="developer", background_hint="dark"),
                LogoChoice(idx=1, role="project", background_hint="light"),
                LogoChoice(idx=6, role="cobrand"),     # dup url of 0 → deduped away
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

        # Logos: dup-url idx 6 dropped → 2 kept, with derived format + background.
        self.assertEqual([l.url for l in out.logos],
                         ["https://x.com/dev-logo.png", "https://x.com/project-logo.webp"])
        self.assertEqual([l.role for l in out.logos], ["developer", "project"])
        self.assertEqual([l.format for l in out.logos], ["png", "webp"])
        self.assertEqual([l.background for l in out.logos], ["dark", "light"])
        self.assertEqual([l.source for l in out.logos], ["jsonld", "og"])

        # creative_image_urls excludes the 'unused' creative.
        self.assertEqual(out.creative_image_urls,
                         ["https://x.com/hero.webp", "https://x.com/pool.jpg",
                          "https://x.com/floorplan.png"])
        # creatives_with_role keeps ALL four (incl. unused), in order.
        self.assertEqual([(c.role) for c in out.creatives_with_role],
                         ["hero", "amenity", "floor_plan", "unused"])

        # Completeness derived by code: hero + >=1 amenity + floor_plan → complete.
        self.assertEqual(out.creative_completeness.verdict, "complete")
        self.assertTrue(out.creative_completeness.hero_found)
        self.assertEqual(out.creative_completeness.amenities_count, 1)
        self.assertTrue(out.creative_completeness.floor_plan_found)
        self.assertEqual(out.creative_completeness.missing_categories, [])

        self.assertEqual(out.confidence, 0.9)

    def test_resolve_picks_drops_out_of_range_indices(self):
        # OOB indices must be silently dropped, not crash.
        sel = AssetSelection(
            logos=[LogoChoice(idx=0, role="main"), LogoChoice(idx=99)],
            creatives=[CreativeChoice(idx=2, role="hero"),
                       CreativeChoice(idx=50, role="amenity")],
            confidence=0.5,
        )
        out = _resolve_picks(sel, CANDS)
        self.assertEqual([l.url for l in out.logos], ["https://x.com/dev-logo.png"])
        self.assertEqual(out.creative_image_urls, ["https://x.com/hero.webp"])
        self.assertEqual([c.role for c in out.creatives_with_role], ["hero"])


if __name__ == "__main__":
    unittest.main()
