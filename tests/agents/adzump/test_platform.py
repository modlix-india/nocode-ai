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

from app.agents.adzump.platform import Platform


class PlatformFromValueTests(unittest.TestCase):

    def test_rows(self):
        # chip labels and realistic real-estate user messages
        google = ["Google Ads", "google", "adwords",
                  "run it on Google Ads for the 3BHK apartments"]
        meta = ["Meta", "facebook", "Facebook Ads", "instagram", "fb", "ig",
                "let's do facebook and instagram for the villa launch"]
        unknown = [None, "", "   ", "linkedin", "tiktok", "the usual", "yes",
                   # the documented bug: a keyword inside another word never matches
                   "the right option please", "signage for the project",
                   "fbi background check", "metaphor", "googleplex tour"]
        for value, expected in ([(v, Platform.GOOGLE) for v in google]
                                + [(v, Platform.META) for v in meta]
                                + [(v, None) for v in unknown]):
            with self.subTest(value=value):
                self.assertIs(Platform.from_value(value), expected)


if __name__ == "__main__":
    unittest.main()
