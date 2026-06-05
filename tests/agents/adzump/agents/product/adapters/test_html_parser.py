"""Regression tests for the HTML candidate-image extractor.

Runs with stdlib unittest — no extra deps needed:

    cd nocode-ai && ./venv/bin/python -m unittest tests.test_html_parser -v

The case that motivated this file: cityville.in renders 107 <img> tags
across 76 unique src URLs (the same logo and a few decorative images
repeat throughout the layout). A prior bug in `_collect_image_candidates`
broke the iteration on the first duplicate-src, dropping ~70 unique
images. The duplicates_before_uniques test is the regression guard.
"""

from __future__ import annotations

import unittest

from app.agents.adzump.agents.product.adapters.html_parser import (
    MAX_IMAGES,
    parse_html,
)


class HtmlParserImageExtractionTests(unittest.TestCase):

    def _html_with_imgs(self, *srcs: str) -> str:
        body = "".join(f'<img src="{s}" alt="">' for s in srcs)
        return f"<html><head><title>t</title></head><body>{body}</body></html>"

    def test_duplicates_do_not_truncate_extraction(self):
        """Real-world failure mode: a page renders the same logo URL across
        <header>, hero overlay, and <footer>. The first duplicate hit must
        not break the rest of the candidate-gathering loop."""
        # Duplicate "logo.png" appears 3x interleaved with 5 unique others.
        html = self._html_with_imgs(
            "https://x.com/logo.png",
            "https://x.com/logo.png",  # duplicate — must NOT stop iteration
            "https://x.com/hero.jpg",
            "https://x.com/logo.png",  # another duplicate
            "https://x.com/p1.jpg",
            "https://x.com/p2.jpg",
            "https://x.com/p3.jpg",
            "https://x.com/p4.jpg",
        )
        page = parse_html("https://x.com/", html)
        srcs = [i.src for i in page.images if i.source == "img"]
        self.assertEqual(len(srcs), len(set(srcs)), "dedupe must hold")
        # All 5 unique URLs survive — the bug was dropping these.
        self.assertSetEqual(
            set(srcs),
            {
                "https://x.com/logo.png",
                "https://x.com/hero.jpg",
                "https://x.com/p1.jpg",
                "https://x.com/p2.jpg",
                "https://x.com/p3.jpg",
                "https://x.com/p4.jpg",
            },
        )

    def test_dedupe_preserves_first_seen(self):
        """When the same src appears twice, only the first one is kept —
        downstream code keeps the richer DOM context (alt/in_header/etc.)
        from the first occurrence."""
        html = (
            "<html><head><title>t</title></head><body>"
            '<header><img src="https://x.com/logo.png" alt="brand"></header>'
            '<footer><img src="https://x.com/logo.png" alt=""></footer>'
            "</body></html>"
        )
        page = parse_html("https://x.com/", html)
        img_candidates = [i for i in page.images if i.source == "img"]
        self.assertEqual(len(img_candidates), 1)
        self.assertEqual(img_candidates[0].alt, "brand")
        self.assertTrue(img_candidates[0].in_header)

    def test_cap_respected(self):
        """Once we hit MAX_IMAGES, iteration stops cleanly."""
        srcs = [f"https://x.com/p{n}.jpg" for n in range(MAX_IMAGES + 20)]
        page = parse_html("https://x.com/", self._html_with_imgs(*srcs))
        self.assertLessEqual(len(page.images), MAX_IMAGES)

    def test_network_images_merged_without_truncation(self):
        """When parse_html is fed a list of URLs the browser loaded but
        which aren't in the DOM, they should append as source='network'
        and not push past the MAX_IMAGES cap."""
        html = self._html_with_imgs(
            "https://x.com/dom1.jpg",
            "https://x.com/dom1.jpg",  # duplicate — must not abort merge
            "https://x.com/dom2.jpg",
        )
        net = [
            {"url": "https://x.com/dom1.jpg"},   # dup of DOM — should be skipped
            {"url": "https://x.com/net1.jpg"},   # new
            {"url": "https://x.com/net2.jpg"},   # new
        ]
        page = parse_html("https://x.com/", html, network_images=net)
        by_source = {}
        for i in page.images:
            by_source.setdefault(i.source, []).append(i.src)
        self.assertEqual(sorted(by_source["img"]),
                         ["https://x.com/dom1.jpg", "https://x.com/dom2.jpg"])
        self.assertEqual(sorted(by_source["network"]),
                         ["https://x.com/net1.jpg", "https://x.com/net2.jpg"])


if __name__ == "__main__":
    unittest.main()
