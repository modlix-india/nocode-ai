"""Lock #1 — `_is_svg_src` (html_parser.py:216), the parser-level SVG filter.

This is the "raster-only candidates by contract" guarantee that `_resolve_picks`
*assumes* (it never expects an .svg to reach it). Real-estate developer logos are
very often SVG (Puravankara, Sobha, etc.), so this filter fires constantly — lock
it so a refactor can't silently let SVGs through to the vision picker.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest \\
        tests.agents.adzump.agents.product.adapters.test_html_parser_svg -v
"""

from __future__ import annotations

import unittest

from app.agents.adzump.agents.product.adapters.html_parser import _is_svg_src


class IsSvgSrcLock(unittest.TestCase):

    def test_svg_sources_are_flagged(self):
        for src in (
            "https://purvasparklingspring.com/img/puravankara-logo.svg",  # dev logo
            "https://sobha.com/assets/sobha-logo.SVG",                    # case-insensitive
            "https://x.com/sparkling-springs-logo.svg?v=2",              # query stripped
            "data:image/svg+xml;base64,PHN2Zy4uLg==",                     # inline data URI
        ):
            with self.subTest(src=src):
                self.assertTrue(_is_svg_src(src))

    def test_raster_and_empty_pass_through(self):
        for src in (
            "",                                                  # empty → not svg
            "https://purvasparklingspring.com/lakefront-hero.webp",
            "https://x.com/3bhk-villa-floor-plan.png",
            "https://x.com/clubhouse.jpg",
            "https://x.com/banner.svgx",        # endswith '.svgx', not '.svg'
            "data:image/png;base64,iVBOR...",   # raster data URI
        ):
            with self.subTest(src=src):
                self.assertFalse(_is_svg_src(src))


if __name__ == "__main__":
    unittest.main()
