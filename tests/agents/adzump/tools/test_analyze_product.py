"""tools/product.py: _normalize_url, _build_llm_summary, tool definition smoke.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump.tools.test_analyze_product -v
"""
from __future__ import annotations

import unittest

from app.agents.adzump.tools.product import (
    BUSINESS_TOOLS, _build_llm_summary, _normalize_url, analyze_business,
)


class NormalizeUrlTests(unittest.TestCase):
    """https forcing for consistent storage keys. Does NOT strip www or
    trailing slashes - that's business_storage's job."""

    def test_table(self):
        for raw, expected in [
            ("https://sobha.com", "https://sobha.com"),
            ("http://x.com/", "https://x.com/"),
            ("purvasparkling.com", "https://purvasparkling.com"),   # naked domain
            ("https://example.com/page/", "https://example.com/page/"),
            ("https://www.example.com", "https://www.example.com"), # www preserved
            ("  https://example.com  ", "https://example.com"),     # whitespace stripped
            ("", "https://"),                                       # empty → bare prefix
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(_normalize_url(raw), expected)


class BuildLlmSummaryTests(unittest.TestCase):
    """Summary shown to the orchestrator LLM - deliberately omits location.
    Location was echoed here and caused the LLM to ask "confirm location?"
    as free text before confirm_location's widget fired (dup question)."""

    def test_table(self):
        full = _build_llm_summary({
            "product_name": "Sobha", "business_type": "real estate",
            "summary": "Premium apartments in Bangalore.",
        })
        for fragment in ("Product: Sobha", "Type: real estate", "Premium apartments"):
            self.assertIn(fragment, full)
        for data, expected in [
            ({}, "Product analysis complete."),
            ({"product_name": "Sobha"}, "Product: Sobha"),
            ({"business_type": "real estate"}, "Type: real estate"),
        ]:
            with self.subTest(data=data):
                self.assertEqual(_build_llm_summary(data), expected)


class ToolDefinitionTests(unittest.TestCase):
    def test_shape(self):
        self.assertEqual(analyze_business.name, "analyze_product")
        self.assertEqual(analyze_business.display_name, "Analyze Product")
        url_param = next(p for p in analyze_business.parameters if p.name == "url")
        self.assertTrue(url_param.required)
        self.assertEqual(BUSINESS_TOOLS, [analyze_business])


if __name__ == "__main__":
    unittest.main()
