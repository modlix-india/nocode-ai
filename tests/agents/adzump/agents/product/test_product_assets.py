"""Prefilter goldens (real parse_html candidates → _prefilter_candidates). svgs
never reach the prefilter (parser drops them). The image-response predicate is
tested with _uploads (test_uploads.py).
The logo-filename guard lives in vision/agent.py - tested in vision/test_agent.py.
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


if __name__ == "__main__":
    unittest.main()
