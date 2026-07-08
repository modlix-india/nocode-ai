"""Lock for Platform.from_value - the word-boundary keyword match (platform.py:50).

Pure deterministic seam: maps a chip label OR a raw user message → Platform.
The `\\b` boundary is the bug-guard: short keywords ("ig", "fb", "meta") must
NOT match as substrings of unrelated words ("right", "fbi", "metaphor") - that
mis-match silently routed campaigns to the wrong platform.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest \\
        tests.agents.adzump.test_platform -v
"""

from __future__ import annotations

import unittest

from app.agents.adzump.platform import (
    Platform, is_google, is_meta, CANONICAL_LABEL,
)


class PlatformFromValueTests(unittest.TestCase):

    def test_chip_labels_and_messages_map(self):
        # (input, expected) - chip labels + realistic real-estate user messages.
        cases = [
            ("Google Ads", Platform.GOOGLE),
            ("google", Platform.GOOGLE),
            ("adwords", Platform.GOOGLE),
            ("run it on Google Ads for the 3BHK apartments", Platform.GOOGLE),
            ("Meta", Platform.META),
            ("facebook", Platform.META),
            ("Facebook Ads", Platform.META),
            ("instagram", Platform.META),
            ("fb", Platform.META),
            ("ig", Platform.META),
            ("let's do facebook and instagram for the villa launch", Platform.META),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertIs(Platform.from_value(value), expected)

    def test_ambiguous_or_unknown_is_none(self):
        for value in (None, "", "   ", "linkedin", "tiktok", "the usual", "yes"):
            with self.subTest(value=value):
                self.assertIsNone(Platform.from_value(value))

    def test_word_boundary_guards_substring_mismatch(self):
        # The documented bug: short keywords must not match inside other words.
        # Each of these CONTAINS a keyword substring but must resolve to None.
        for value in (
            "the right option please",   # 'ig' inside 'right'
            "signage for the project",   # 'ig' inside 'signage'
            "fbi background check",      # 'fb' inside 'fbi'
            "metaphor",                  # 'meta' inside 'metaphor'
            "googleplex tour",           # 'google' is standalone here? -> guard below
        ):
            with self.subTest(value=value):
                # 'googleplex' has no boundary after 'google' -> must NOT match.
                self.assertIsNone(Platform.from_value(value))

    def test_helpers_and_canonical_labels(self):
        self.assertTrue(is_google("Google Ads"))
        self.assertFalse(is_google("Meta"))
        self.assertTrue(is_meta("instagram"))
        self.assertFalse(is_meta("adwords"))
        self.assertEqual(CANONICAL_LABEL[Platform.GOOGLE], "Google Ads")
        self.assertEqual(CANONICAL_LABEL[Platform.META], "Meta")


if __name__ == "__main__":
    unittest.main()
