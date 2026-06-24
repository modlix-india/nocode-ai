"""Tests for app/agents/adzump/tools/product.py

Covers:
  1. _normalize_url — https forcing for consistent storage keys
  2. _build_llm_summary — tool-result summary (what orchestrator LLM sees)
  3. ToolDefinition loads without errors

Run:
    cd nocode-ai && ./venv/bin/python -m unittest \\
        tests.agents.adzump.tools.test_analyze_product -v
"""

from __future__ import annotations

import unittest

from app.agents.adzump.tools.product import (
    _normalize_url,
    _build_llm_summary,
    analyze_business,
    BUSINESS_TOOLS,
)


class NormalizeUrl(unittest.TestCase):
    """https forcing for consistent storage keys.

    Does NOT strip www or trailing slashes — that's business_storage's job.
    """

    def test_https_unchanged(self):
        self.assertEqual(_normalize_url("https://sobha.com"), "https://sobha.com")

    def test_http_becomes_https(self):
        self.assertEqual(_normalize_url("http://x.com/"), "https://x.com/")

    def test_naked_domain_gets_https(self):
        self.assertEqual(_normalize_url("purvasparkling.com"), "https://purvasparkling.com")

    def test_preserves_trailing_slash(self):
        self.assertEqual(_normalize_url("https://example.com/page/"), "https://example.com/page/")

    def test_preserves_www(self):
        # www is NOT stripped here — business_storage._normalize_url does that.
        self.assertEqual(_normalize_url("https://www.example.com"), "https://www.example.com")

    def test_whitespace_stripped(self):
        self.assertEqual(_normalize_url("  https://example.com  "), "https://example.com")

    def test_empty_string_becomes_https(self):
        # No scheme + empty → bare domain gets https:// prepended.
        self.assertEqual(_normalize_url(""), "https://")


class BuildLlmSummary(unittest.TestCase):
    """Summary shown to the orchestrator LLM — deliberately omits location.

    Location was echoed here and caused the LLM to ask "confirm location?"
    as free text before confirm_location's widget fired (dup question).
    """

    def test_full_fields(self):
        result = _build_llm_summary({
            "product_name": "Sobha",
            "business_type": "real estate",
            "summary": "Premium apartments in Bangalore.",
        })
        self.assertIn("Product: Sobha", result)
        self.assertIn("Type: real estate", result)
        self.assertIn("Premium apartments", result)

    def test_missing_fields_omitted(self):
        result = _build_llm_summary({})
        self.assertEqual(result, "Product analysis complete.")

    def test_only_name(self):
        result = _build_llm_summary({"product_name": "Sobha"})
        self.assertEqual(result, "Product: Sobha")

    def test_only_type(self):
        result = _build_llm_summary({"business_type": "real estate"})
        self.assertEqual(result, "Type: real estate")


class ToolDefinition(unittest.TestCase):
    """Smoke: the tool definition loads and has the right shape."""

    def test_analyze_business_name(self):
        self.assertEqual(analyze_business.name, "analyze_product")

    def test_display_name(self):
        self.assertEqual(analyze_business.display_name, "Analyze Product")

    def test_url_param_required(self):
        url_param = next(p for p in analyze_business.parameters if p.name == "url")
        self.assertTrue(url_param.required)

    def test_business_tools_has_one_entry(self):
        self.assertEqual(len(BUSINESS_TOOLS), 1)
        self.assertIs(BUSINESS_TOOLS[0], analyze_business)


if __name__ == "__main__":
    unittest.main()
