"""Regression tests for the product-assets prefilter.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.test_product_assets -v
"""

from __future__ import annotations

import unittest

from app.agents.adzump.agents.product.models import SiteImage
from app.agents.adzump.agents.product.product_assets import _prefilter_candidates


def _img(src: str, source: str = "img") -> SiteImage:
    return SiteImage(src=src, source=source)


class PrefilterTests(unittest.TestCase):

    # STALE: v9 (2026-05-22) retired the prefilter SVG-penalty — html_parser now
    # filters SVGs upstream, so they never reach _prefilter_candidates. This feeds
    # SVGs straight in (an input that can't occur) and asserts a path that's gone.
    # The crowding concern needs re-homing to a html_parser SVG-filter test (untested today).
    @unittest.skip("v9 retired the prefilter SVG-penalty; SVGs filtered upstream in html_parser")
    def test_svg_icons_do_not_crowd_out_real_photos(self):
        """The subhavillamor failure pattern: 30+ decorative SVG icons live
        as <img> in the DOM alongside a handful of real product photos.
        With a top_n=25 cap, all the real photos must still surface — the
        SVG icons must sit behind them in the sort."""
        candidates = []
        # 30 SVG decorative icons — all in 'img' source.
        for n in range(30):
            candidates.append(_img(f"https://x.com/icon_{n}.svg"))
        # 8 real product photos sprinkled later in DOM order (positions 30-37).
        photo_urls = [
            "https://x.com/masterplan.webp",
            "https://x.com/clubhouse.webp",
            "https://x.com/3bhk_floor.webp",
            "https://x.com/3bhk_floor_2.webp",
            "https://x.com/herobg.jpg",
            "https://x.com/entrance.jpg",
            "https://x.com/exterior.png",
            "https://x.com/garden.jpg",
        ]
        for url in photo_urls:
            candidates.append(_img(url, source="img"))
        # 2 network-source SVG noise at the end.
        candidates.append(_img("https://x.com/network_icon.svg", source="network"))
        candidates.append(_img("https://x.com/network_real.webp", source="network"))

        kept = _prefilter_candidates(candidates, top_n=25)
        kept_srcs = {c.src for c in kept}
        for photo in photo_urls:
            self.assertIn(photo, kept_srcs,
                          f"real photo {photo} was crowded out by SVG icons")
        # Network non-SVG also makes it (penalty 0, source priority lower).
        self.assertIn("https://x.com/network_real.webp", kept_srcs)

    def test_jsonld_and_link_svgs_are_not_penalized(self):
        """Brand-logo SVGs from JSON-LD / og / link sources are legitimate
        vector logos — they must NOT get the SVG icon penalty."""
        candidates = [
            _img("https://x.com/photo1.jpg", source="img"),
            _img("https://x.com/photo2.jpg", source="img"),
            _img("https://x.com/logo.svg", source="jsonld"),    # brand logo
            _img("https://x.com/og_logo.svg", source="og"),     # brand logo
            _img("https://x.com/apple-touch.svg", source="link"),  # brand logo
            _img("https://x.com/icon.svg", source="img"),       # decorative
        ]
        # Cap at 4 so we force a ranking decision.
        kept = _prefilter_candidates(candidates, top_n=4)
        kept_srcs = {c.src for c in kept}
        # JSON-LD beats img — both photos AND the jsonld SVG should be in.
        self.assertIn("https://x.com/logo.svg", kept_srcs)
        # Decorative img-source SVG should sit behind everything else and
        # get dropped at top_n=4.
        self.assertNotIn("https://x.com/icon.svg", kept_srcs)

    def test_preserves_insertion_order_within_kept(self):
        """The kept candidates should appear in their original DOM/insertion
        order so the LLM sees them roughly as the page laid them out."""
        candidates = [
            _img("https://x.com/a.jpg"),
            _img("https://x.com/b.svg"),
            _img("https://x.com/c.jpg"),
        ]
        kept = _prefilter_candidates(candidates, top_n=3)
        self.assertEqual(
            [c.src for c in kept],
            ["https://x.com/a.jpg", "https://x.com/b.svg", "https://x.com/c.jpg"],
        )

    def test_no_op_when_under_cap(self):
        candidates = [_img(f"https://x.com/{n}.jpg") for n in range(5)]
        kept = _prefilter_candidates(candidates, top_n=10)
        self.assertEqual(len(kept), 5)
        self.assertEqual([c.src for c in kept], [c.src for c in candidates])


class LooksLikeImageResponseTests(unittest.TestCase):
    """Regression: some CDNs (cdn.modlix.com on earthen) serve image bytes
    without setting a Content-Type header. Browsers fall back to the URL
    extension; we must too, or we drop perfectly valid images."""

    def test_proper_image_content_type_passes(self):
        from app.agents.adzump._uploads import looks_like_image_response
        self.assertTrue(looks_like_image_response("image/jpeg", "https://x/y.jpg"))
        self.assertTrue(looks_like_image_response("image/webp; charset=binary", "https://x/y.webp"))
        self.assertTrue(looks_like_image_response("IMAGE/PNG", "https://x/y.png"))

    def test_missing_content_type_falls_back_to_url_extension(self):
        from app.agents.adzump._uploads import looks_like_image_response
        # The earthen case: HTTP 200, no Content-Type header, URL ends in .jpg.
        self.assertTrue(looks_like_image_response("", "https://cdn.x/A01-8K-Entrance-copy.jpg"))
        self.assertTrue(looks_like_image_response("", "https://cdn.x/Banner-Image%201.jpg"))
        self.assertTrue(looks_like_image_response(None, "https://cdn.x/photo.webp"))
        self.assertTrue(looks_like_image_response("", "https://cdn.x/p.PNG?cache=1"))

    def test_non_image_content_type_always_rejected(self):
        """An explicit non-image content-type wins over a misleading URL ext."""
        from app.agents.adzump._uploads import looks_like_image_response
        self.assertFalse(looks_like_image_response("text/html", "https://x/y.jpg"))
        self.assertFalse(looks_like_image_response("application/json", "https://x/y.png"))

    def test_missing_content_type_without_image_extension_rejected(self):
        from app.agents.adzump._uploads import looks_like_image_response
        self.assertFalse(looks_like_image_response("", "https://x/some-page"))
        self.assertFalse(looks_like_image_response("", "https://x/track?id=1"))


class FilenameSuggestsLogoTests(unittest.TestCase):
    """The post-LLM-pick safety net for the 'creative accidentally includes
    a sub-brand wordmark' pattern (clublogo.png / partnerlogo.svg / etc.)."""

    def test_logo_substring_matches(self):
        from app.agents.adzump.agents.product.product_assets import (
            _filename_suggests_logo,
        )
        cases = [
            "https://x.com/clublogo.png",
            "https://x.com/CLUBLOGO.png",
            "https://x.com/club_logo.png",
            "https://x.com/brand-logo-white.svg",
            "https://x.com/logos/main.png",  # path token 'logos' but filename 'main' — should NOT match
            "https://x.com/wordmark_dark.svg",
            "https://x.com/products/villa-1.jpg",
        ]
        results = [(u, _filename_suggests_logo(u)) for u in cases]
        # Match: anything with 'logo' or 'wordmark' in the filename token.
        self.assertTrue(results[0][1])   # clublogo.png
        self.assertTrue(results[1][1])   # CLUBLOGO.png (case-insensitive)
        self.assertTrue(results[2][1])   # club_logo.png
        self.assertTrue(results[3][1])   # brand-logo-white.svg
        self.assertFalse(results[4][1])  # logos/ in PATH but filename is 'main' — no match
        self.assertTrue(results[5][1])   # wordmark_dark.svg
        self.assertFalse(results[6][1])  # villa-1.jpg — clearly a product


if __name__ == "__main__":
    unittest.main()
