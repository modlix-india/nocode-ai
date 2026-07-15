"""Golden-text guard for the keyword phase prompts
(app/agents/adzump/agents/campaign/google/keyword/{funnels,context}.py).

The fixture was captured from the ORIGINAL (phase, KeywordType) registry BEFORE the funnel
refactor, rendered exactly as `agent.build_turn_reminder` renders it. Every funnel/phase
prompt must stay byte-identical through the refactor:

  A1  moved the six guidance strings verbatim into FunnelSpec  -> this passes trivially
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
from app.agents.adzump.agents.campaign.google.keyword.context import Phase, phase_prompt
from app.agents.adzump.agents.campaign.google.keyword.funnels import FUNNELS

_GOLDEN = Path(__file__).parent / "fixtures" / "golden_keyword_prompts.json"


def _render(phase: Phase, funnel) -> str:
    """Render exactly as agent.build_turn_reminder does (agent.py:143-148)."""
    return Template(phase_prompt(phase, funnel)).safe_substitute(
        max_seeds=constants.MAX_SEEDS,
        target_count=constants.TARGET_POSITIVE_COUNT,
        max_negatives=constants.MAX_NEGATIVE_COUNT,
    )


class GoldenPromptTests(unittest.TestCase):
    def setUp(self):
        self.golden: dict[str, str] = json.loads(_GOLDEN.read_text())

    def test_rendered_prompts_are_byte_identical_to_the_pre_refactor_registry(self):
        for key, expected in self.golden.items():
            phase_value, funnel_id = key.split("|")
            with self.subTest(phase=phase_value, funnel=funnel_id):
                actual = _render(Phase(phase_value), FUNNELS[funnel_id])
                self.assertEqual(actual, expected)

    def test_golden_covers_every_funnel_and_phase(self):
        # Guards the guard: a funnel added without a fixture would otherwise pass silently.
        expected_keys = {f"{p.value}|{fid}" for p in Phase for fid in FUNNELS}
        self.assertEqual(set(self.golden), expected_keys)


class FunnelGuidanceTests(unittest.TestCase):
    def test_every_funnel_has_guidance_for_every_phase(self):
        # Mirrors the import-time guard in context.py — the data-level replacement for the
        # old (phase x type) registry completeness check.
        for funnel in FUNNELS.values():
            for phase in Phase:
                with self.subTest(funnel=funnel.id, phase=phase.value):
                    self.assertTrue(phase_prompt(phase, funnel).strip())

    def test_registry_is_keyed_by_funnel_id(self):
        for fid, funnel in FUNNELS.items():
            self.assertEqual(fid, funnel.id)
            self.assertTrue(funnel.label)

    def test_behaviour_flags_match_the_guidance_they_encode(self):
        # These flags exist so the policy stated in the prompt and the policy enforced in
        # code come from one place — assert they agree with the guidance text.
        brand, generic = FUNNELS["brand"], FUNNELS["generic"]
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
