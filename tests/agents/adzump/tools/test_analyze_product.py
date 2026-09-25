"""tools/product.py: _normalize_url, _build_llm_summary, _restored_facts, tool
definition smoke.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump.tools.test_analyze_product -v
"""
from __future__ import annotations

import unittest

from app.agents.adzump.tools.product import (
    BUSINESS_TOOLS, _build_llm_summary, _normalize_url, _restored_facts,
    analyze_business,
)


class NormalizeUrlTests(unittest.TestCase):
    """https forcing for consistent storage keys. Does NOT strip www or
    trailing slashes - that's normalize_business_url's job."""

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


class RestoredFactsTests(unittest.TestCase):
    """What a resume tells the model it brought back - only what the store held
    (live 2026-09-23: the old line read an empty product.location)."""

    def test_table(self):
        full = {
            "product_data": {
                "product_name": "Misty Shores", "business_type": "Residential",
                "place": {"address": "Near ITPB (Whitefield)", "lat": 12.97},
                "target_areas": [{}] * 17,
                "ad_accounts": {"meta": {"parent_account": "B1", "account": "A1",
                                         "names": {"B1": "AdZump Dummy"}}},
            },
            "competitor_analysis": {"competitors": [
                {"name": "Brigade Avalon", "creatives": [{}]}, {"name": "Godrej United"}]},
        }
        facts = _restored_facts(full)
        self.assertEqual(len(facts), 5)  # product, location, competitors, areas, accounts
        text = " | ".join(facts)
        for value in ("Misty Shores", "Near ITPB (Whitefield)", "17", "AdZump Dummy", "A1"):
            with self.subTest(value=value):
                self.assertIn(value, text)
        self.assertNotIn("B1", text)  # an account's display name wins over its id
        self.assertEqual(len(_restored_facts({"product_data": {"product_name": "X"}})), 1)


if __name__ == "__main__":
    unittest.main()
