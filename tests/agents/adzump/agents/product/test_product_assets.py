"""Prefilter goldens (real parse_html candidates → _prefilter_candidates) + the
image-response / filename predicates. svgs never reach the prefilter (parser drops them).
Bless: BLESS_FIXTURES=1 venv/bin/python -m unittest <this module>"""

from __future__ import annotations

import unittest

from app.agents.adzump.agents.product.product_assets import (
    TOP_N_CANDIDATES,
    _prefilter_candidates,
)
from tests.agents.adzump import fixtures


class PrefilterGoldenTests(unittest.TestCase):
    pass  # one test per fixture, added below


def _add_tests() -> None:
    for fx_path in fixtures.inputs():
        name = fixtures.name(fx_path)

        def test(self, fx_path=fx_path):
            kept = _prefilter_candidates(fixtures.parsed(fx_path).images, TOP_N_CANDIDATES)
            got = [{"src": c.src, "source": c.source} for c in kept]
            fixtures.check(self, got, fx_path, "prefilter")
            # cap is the prefilter's whole job - never exceed it.
            self.assertLessEqual(len(got), TOP_N_CANDIDATES, f"{name}: over TOP_N cap")

        setattr(PrefilterGoldenTests, f"test_{name}", test)


_add_tests()


class LooksLikeImageResponseTests(unittest.TestCase):
    """Some CDNs serve image bytes with no Content-Type; fall back to URL ext or
    we drop valid images. Type edge-cases real fixtures don't carry - kept as units."""

    def test_proper_image_content_type_passes(self):
        from app.agents.adzump._uploads import looks_like_image_response
        self.assertTrue(looks_like_image_response("image/jpeg", "https://x/y.jpg"))
        self.assertTrue(looks_like_image_response("image/webp; charset=binary", "https://x/y.webp"))
        self.assertTrue(looks_like_image_response("IMAGE/PNG", "https://x/y.png"))

    def test_missing_content_type_falls_back_to_url_extension(self):
        from app.agents.adzump._uploads import looks_like_image_response
        # HTTP 200, no Content-Type header, URL ends in .jpg.
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
    """Safety net: creative whose filename is a sub-brand wordmark (clublogo.png etc.)."""

    def test_logo_substring_matches(self):
        from app.agents.adzump.agents.product.product_assets import (
            _filename_suggests_logo,
        )
        cases = [
            "https://x.com/clublogo.png",
            "https://x.com/CLUBLOGO.png",
            "https://x.com/club_logo.png",
            "https://x.com/brand-logo-white.svg",
            "https://x.com/logos/main.png",  # path token 'logos' but filename 'main' - should NOT match
            "https://x.com/wordmark_dark.svg",
            "https://x.com/products/villa-1.jpg",
        ]
        results = [(u, _filename_suggests_logo(u)) for u in cases]
        # Match: anything with 'logo' or 'wordmark' in the filename token.
        self.assertTrue(results[0][1])   # clublogo.png
        self.assertTrue(results[1][1])   # CLUBLOGO.png (case-insensitive)
        self.assertTrue(results[2][1])   # club_logo.png
        self.assertTrue(results[3][1])   # brand-logo-white.svg
        self.assertFalse(results[4][1])  # logos/ in PATH but filename is 'main' - no match
        self.assertTrue(results[5][1])   # wordmark_dark.svg
        self.assertFalse(results[6][1])  # villa-1.jpg - clearly a product


if __name__ == "__main__":
    unittest.main()
