"""Golden-text guard for the keyword phase prompts
(app/agents/adzump/agents/campaign/google/keyword/{themes,context}.py).

The fixture was captured from the ORIGINAL (phase, KeywordType) registry BEFORE the theme
refactor, rendered exactly as `agent.build_turn_reminder` renders it. Every theme/phase
prompt must stay byte-identical through the refactor:

  A1  moved the six guidance strings verbatim into KeywordTheme  -> this passes trivially
  A2  factors the shared skeleton into templates               -> this protects the factoring

A diff here means the model's instructions changed. That is either a real regression, or an
intentional prompt edit — in which case re-capture the fixture in the SAME commit, so the
prompt change is reviewable on its own rather than hidden inside a refactor.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from string import Template

from app.agents.adzump.agents.campaign.google.keyword import constants
from app.agents.adzump.agents.campaign.google.keyword.context import (
    BUILD_PHASES,
    Phase,
    phase_prompt,
)
from app.agents.adzump.agents.campaign.google.keyword.themes import KEYWORD_THEMES

_GOLDEN = Path(__file__).parent / "fixtures" / "golden_keyword_prompts.json"


def _render(phase: Phase, theme, user_message: str = "") -> str:
    """Render exactly as agent.build_turn_reminder does."""
    return Template(phase_prompt(phase, theme)).safe_substitute(
        max_seeds=constants.MAX_SEEDS,
        target_count=constants.TARGET_POSITIVE_COUNT,
        max_negatives=constants.MAX_NEGATIVE_COUNT,
        select_guidance=theme.select_guidance,
        user_message=user_message,
    )


class GoldenPromptTests(unittest.TestCase):
    def setUp(self):
        self.golden: dict[str, str] = json.loads(_GOLDEN.read_text())

    def test_rendered_prompts_are_byte_identical_to_the_pre_refactor_registry(self):
        for key, expected in self.golden.items():
            phase_value, theme_id = key.split("|")
            with self.subTest(phase=phase_value, theme=theme_id):
                actual = _render(Phase(phase_value), KEYWORD_THEMES[theme_id])
                self.assertEqual(actual, expected)

    def test_golden_covers_every_theme_and_build_phase(self):
        # Guards the guard: a theme added without a fixture would otherwise pass silently.
        # Only the BUILD phases are pinned — MANAGE is shared and post-dates the fixture.
        expected_keys = {f"{p.value}|{fid}" for p in BUILD_PHASES for fid in KEYWORD_THEMES}
        self.assertEqual(set(self.golden), expected_keys)


class ThemeGuidanceTests(unittest.TestCase):
    def test_every_theme_has_guidance_for_every_build_phase(self):
        # Mirrors the import-time guard in context.py — the data-level replacement for the
        # old (phase x type) registry completeness check.
        for theme in KEYWORD_THEMES.values():
            for phase in BUILD_PHASES:
                with self.subTest(theme=theme.id, phase=phase.value):
                    self.assertTrue(phase_prompt(phase, theme).strip())

    def test_manage_is_shared_and_carries_the_theme_selection_bar(self):
        # An edit must clear the same bar the build used, so MANAGE renders the theme's own
        # select_guidance — that's what stops an added keyword drifting below the standard.
        for theme in KEYWORD_THEMES.values():
            with self.subTest(theme=theme.id):
                rendered = _render(Phase.MANAGE, theme)
                self.assertIn("lookup_keyword", rendered)      # answer from the record
                self.assertIn("edit_keywords", rendered)       # never re-submit a set
                self.assertIn(theme.select_guidance, rendered)  # the build's own bar

    def test_registry_is_keyed_by_theme_id(self):
        for fid, theme in KEYWORD_THEMES.items():
            self.assertEqual(fid, theme.id)
            self.assertTrue(theme.label)

    def test_behaviour_flags_match_the_guidance_they_encode(self):
        # These flags exist so the policy stated in the prompt and the policy enforced in
        # code come from one place — assert they agree with the guidance text.
        brand, generic = KEYWORD_THEMES["brand"], KEYWORD_THEMES["generic"]
        self.assertTrue(brand.keep_zero_volume)          # "own these even at low or zero volume"
        self.assertIn("at low or zero", brand.select_guidance)
        self.assertIn("Keep brand terms at zero", brand.select_guidance)
        self.assertFalse(generic.keep_zero_volume)       # "drop 0-volume terms"
        self.assertIn("drop 0-volume terms", generic.select_guidance)
        self.assertTrue(generic.allows_cross_business)   # is_cross_business is generic-only
        self.assertIn("is_cross_business", generic.select_guidance)
        self.assertFalse(brand.allows_cross_business)
        self.assertTrue(brand.requires_brand_token)      # brand ELIGIBILITY rule
        self.assertFalse(generic.requires_brand_token)


if __name__ == "__main__":
    unittest.main()
