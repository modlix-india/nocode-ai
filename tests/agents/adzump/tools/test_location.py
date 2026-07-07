"""location._detected_location - the place.address accessor (wire shapes are
normalized into place at the merge boundary, tools/product.py).

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump.tools.test_location -v
"""
from __future__ import annotations

import unittest

from app.agents.adzump.tools.location import _detected_location


class DetectedLocationTests(unittest.TestCase):
    def test_table(self):
        for product, expected in [
            ({"place": {"address": "Bengaluru"}}, "Bengaluru"),
            ({"place": {"address": "  Pune  "}}, "Pune"),
            ({"place": {}}, ""),
            ({"place": None}, ""),
            ({}, ""),
        ]:
            with self.subTest(product=product):
                self.assertEqual(_detected_location(product), expected)


if __name__ == "__main__":
    unittest.main()
