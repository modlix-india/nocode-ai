"""contains_normalized — the normalized substring de-dup shared by the run loop
(audience="both" relay) and present_options (F9 question de-dup). Pure.

    cd nocode-ai && ./venv/bin/python -m unittest tests.core.test_text -v
"""
from __future__ import annotations

import unittest

from app.core.text import contains_normalized


class ContainsNormalizedTests(unittest.TestCase):

    def test_exact_match(self):
        self.assertTrue(contains_normalized("How long should it run?", "How long should it run?"))

    def test_case_and_whitespace_insensitive(self):
        self.assertTrue(contains_normalized("How long should it run?", "ok... HOW   long should it run"))

    def test_trailing_punctuation_stripped_both_sides(self):
        # the divergence the first impl missed: needle ends in '.', echo dropped it.
        self.assertTrue(contains_normalized(
            "Found 2 competitors: Sobha, Prestige.",
            "Found 2 competitors: Sobha, Prestige and more details"))

    def test_needle_as_substring_of_haystack(self):
        self.assertTrue(contains_normalized("save your logo", "Done — save your logo, then continue."))

    def test_divergent_paraphrase_does_not_match(self):
        self.assertFalse(contains_normalized(
            "How long should it run?", "How many days do you want this for?"))

    def test_empty_needle_is_false(self):
        self.assertFalse(contains_normalized("", "anything at all"))
        self.assertFalse(contains_normalized("   ?? ", "anything"))   # normalizes to empty

    def test_empty_haystack_is_false(self):
        self.assertFalse(contains_normalized("something", ""))


if __name__ == "__main__":
    unittest.main()
