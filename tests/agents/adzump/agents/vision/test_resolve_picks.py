"""Characterization golden for _resolve_picks — the deterministic seam
BELOW the vision model (real logic, no mocks, no model call).

The analyst's model returns an AssetSelection (indices + roles); _resolve_picks
maps that onto ProductAssets (urls + derived completeness). We hand-author the
selection (the model's hypothetical output) and lock the mapping, so design C's
"generalize the select" step can't silently change scrape's picks.

Fixture = a real scrape of **Purva Sparkling Springs** (purvasparklingspring.com,
our canonical real-estate test product): developer + project logos, a lakefront
elevation hero, a clubhouse amenity, a 3BHK floor plan, and a RERA disclaimer
banner (the quintessential real-estate "unused" creative). idx 6 re-lists the
developer logo url to exercise dedup. Raster-only: html_parser drops every SVG
at the parser (v9 — _is_svg_src, "raster-only candidates by contract"), so an
.svg never reaches _resolve_picks; logos here are png/webp accordingly.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest \
        tests.agents.adzump.agents.vision.test_resolve_picks -v
"""

from __future__ import annotations

import unittest

from app.agents.adzump.agents.vision.agent import _resolve_picks
from app.agents.adzump.agents.vision.models import (
    AssetSelection, LogoChoice, CreativeChoice,
)
from app.agents.adzump.agents.product.models import SiteImage

_SITE = "https://purvasparklingspring.com/img"

DEV_LOGO = f"{_SITE}/puravankara-logo.png"          # 0 — developer (parent) logo
PROJ_LOGO = f"{_SITE}/sparkling-springs-logo.webp"  # 1 — project logo
HERO = f"{_SITE}/lakefront-elevation.webp"          # 2 — hero render
AMENITY = f"{_SITE}/clubhouse-infinity-pool.jpg"    # 3 — amenity
FLOOR_PLAN = f"{_SITE}/3bhk-villa-floor-plan.png"   # 4 — floor plan
RERA_BANNER = f"{_SITE}/rera-disclaimer-banner.jpg" # 5 — unused (RERA junk)


def _img(src: str, source: str = "img") -> SiteImage:
    return SiteImage(src=src, source=source)


CANDS = [
    _img(DEV_LOGO, "jsonld"),    # 0 — developer logo (Organization.logo)
    _img(PROJ_LOGO, "og"),       # 1 — project logo (og:image)
    _img(HERO),                  # 2 — hero
    _img(AMENITY),               # 3 — amenity
    _img(FLOOR_PLAN),            # 4 — floor plan
    _img(RERA_BANNER),           # 5 — unused
    _img(DEV_LOGO),              # 6 — DUP url of idx 0
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
        self.assertEqual([l.url for l in out.logos], [DEV_LOGO, PROJ_LOGO])
        self.assertEqual([l.role for l in out.logos], ["developer", "project"])
        self.assertEqual([l.format for l in out.logos], ["png", "webp"])
        self.assertEqual([l.background for l in out.logos], ["dark", "light"])
        self.assertEqual([l.source for l in out.logos], ["jsonld", "og"])

        # creative_image_urls excludes the 'unused' RERA banner.
        self.assertEqual(out.creative_image_urls, [HERO, AMENITY, FLOOR_PLAN])
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
        self.assertEqual([l.url for l in out.logos], [DEV_LOGO])
        self.assertEqual(out.creative_image_urls, [HERO])
        self.assertEqual([c.role for c in out.creatives_with_role], ["hero"])


if __name__ == "__main__":
    unittest.main()
