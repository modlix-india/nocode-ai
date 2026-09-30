"""Unit tests for keyword models
(app/agents/adzump/agents/campaign/google/keyword/models.py) —
BusinessProfile.source_names per-keyword-type autosuggest selection.
"""

# regression: YouTube (informational intent) must join only the generic run, never
# the brand (bottom-funnel) run; the web-search defaults are always the primary set.
from __future__ import annotations

import unittest

from app.agents.adzump.agents.campaign.google.keyword.models import BusinessProfile

_DEFAULTS = ["google", "bing", "duckduckgo"]


class SourceNamesTests(unittest.TestCase):
    def test_defaults_only_without_informational_funnel(self):
        p = BusinessProfile(category="saas")  # informational funnel off
        self.assertEqual(p.source_names("brand"), _DEFAULTS)
        self.assertEqual(p.source_names("generic"), _DEFAULTS)

    def test_youtube_added_only_for_generic_when_informational(self):
        p = BusinessProfile(category="saas", includes_informational_funnel=True)
        # brand is bottom-funnel — YouTube is off-intent there
        self.assertEqual(p.source_names("brand"), _DEFAULTS)
        self.assertNotIn("youtube", p.source_names("brand"))
        # generic + informational funnel — YouTube appended after the defaults
        self.assertEqual(p.source_names("generic"), _DEFAULTS + ["youtube"])
        self.assertEqual(p.source_names("generic")[-1], "youtube")

    def test_theme_id_is_required(self):
        # No default: guessing a theme would silently query the wrong surfaces.
        p = BusinessProfile(category="saas", includes_informational_funnel=True)
        with self.assertRaises(TypeError):
            p.source_names()

    def test_unknown_theme_raises(self):
        p = BusinessProfile(category="saas")
        with self.assertRaises(KeyError):
            p.source_names("nonexistent")


if __name__ == "__main__":
    unittest.main()
