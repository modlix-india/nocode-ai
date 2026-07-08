"""targeting_run helpers - the prompt the model plans from, list rendering.

The agent's accuracy depends on this rendering: a wrong index map breaks the
model's ability to turn 'the second area' into delete_location(index=2).
build_run_result gating is covered end-to-end in test_agent (it needs the
run/session interplay); the pure rendering contracts are locked here.
"""
from __future__ import annotations

import unittest

from app.agents.adzump.agents.location.targeting_run import (
    build_run_prompt,
    format_current_areas,
)


class CurrentAreasFormatTests(unittest.TestCase):
    def test_empty_areas_render_explicit_marker(self):
        self.assertIn("empty", format_current_areas([]).lower())

    def test_one_based_indexing(self):
        text = format_current_areas([{"name": "Andheri"}, {"name": "Juhu"}])
        self.assertIn("1. Andheri", text)
        self.assertIn("2. Juhu", text)
        # Zero-based numbering would be a silent bug - verify we don't have it.
        self.assertNotIn("0. Andheri", text)

    def test_unnamed_areas_use_marker(self):
        text = format_current_areas([{}, {"name": "Juhu"}])
        self.assertIn("1. (unnamed)", text)
        self.assertIn("2. Juhu", text)


class RunPromptTests(unittest.TestCase):
    def test_prompt_carries_profile_list_and_verbatim_request(self):
        product = {
            "product_name": "Purva Heights",
            "business_type": "Real Estate",
            "business_scale": "Local",
            "target_areas": [{"name": "Andheri"}],
            "summary": "Premium 3BHK apartments.",
        }
        prompt = build_run_prompt(product, "Mumbai, India", "IN", "add Juhu")
        self.assertIn("Purva Heights", prompt)
        self.assertIn("local", prompt)          # scale is normalized lowercase
        self.assertIn("Mumbai, India", prompt)
        self.assertIn("1. Andheri", prompt)
        self.assertIn('"""add Juhu"""', prompt)

    def test_prompt_truncates_long_summaries(self):
        prompt = build_run_prompt(
            {"summary": "x" * 1000}, "", "IN", "set targeting")
        self.assertNotIn("x" * 700, prompt)


if __name__ == "__main__":
    unittest.main()
