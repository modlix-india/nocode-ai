"""Unit: app/agents/adzump/answer_parse.py — parse_typed_answer."""
from __future__ import annotations

import unittest

from app.agents.adzump.answer_parse import parse_typed_answer


class ParserTableTests(unittest.TestCase):
    def test_table(self):
        cases = [
            ("duration", "25 days", "$", "25 days"),
            ("duration", "a week", "$", "1 week"),
            ("duration", "2 months", "$", "2 months"),
            ("duration", "30 days no make it 60", "$", None),
            ("duration", "asap", "$", None),
            ("budget", "4k", "₹", "₹4,000/day"),
            ("budget", "₹4000/day", "₹", "₹4,000/day"),
            ("budget", "4000", "₹", None),
            ("budget", "$50", "₹", "$50/day"),
            ("platform", "facebook", "$", "Meta"),
            ("platform", "not google", "$", None),
            ("platform", "google or meta", "$", None),
            ("competitive_analysis_declined", "No", "$", None),  # not parseable; chip-only
        ]
        for field, text, cur, exp in cases:
            self.assertEqual(parse_typed_answer(field, text, cur), exp, f"{field} {text!r}")


if __name__ == "__main__":
    unittest.main()
