"""Unit tests for Lead Form prompts and context builder."""

import unittest

from app.agents.adzump.agents.leadform.context import (
    BASE_GENERATE,
    BASE_MANAGE,
    Phase,
    _PHASE_PROMPTS,
    phase_prompt,
)
from app.agents.adzump.agents.leadform.models import (
    MAX_CONTEXT_CARD_BULLET_LENGTH,
    MAX_CONTEXT_CARD_BULLETS_COUNT,
    MAX_CONTEXT_CARD_TITLE_LENGTH,
    MAX_CTA_BUTTON_TEXT_LENGTH,
    MAX_CUSTOM_QUESTIONS_COUNT,
    MAX_FORM_NAME_LENGTH,
    MAX_PRIVACY_LINK_TEXT_LENGTH,
    MAX_QUESTION_PAGE_HEADLINE_LENGTH,
    MAX_THANK_YOU_DESCRIPTION_LENGTH,
    MAX_THANK_YOU_HEADLINE_LENGTH,
)


class TestLeadFormContextPrompts(unittest.TestCase):
    """Verifies prompt generation, dynamic constraint interpolation, and markdown formatting."""

    def test_prompts_contain_no_box_drawing_characters(self):
        """Ensure no expensive non-ASCII box-drawing characters exist in prompts."""
        for phase, prompt in _PHASE_PROMPTS.items():
            self.assertNotIn(
                "\u2550",
                prompt,
                f"Phase {phase.value} prompt contains unicode box-drawing character U+2550",
            )
            self.assertNotIn(
                "═",
                prompt,
                f"Phase {phase.value} prompt contains unicode box-drawing character '═'",
            )

    def test_recommend_prompt_interpolates_model_constraints(self):
        """Ensure RECOMMEND prompt dynamically binds validation limits from models constants."""
        prompt = phase_prompt(Phase.RECOMMEND)

        self.assertIn(f"Form Name: ≤ {MAX_FORM_NAME_LENGTH} chars", prompt)
        self.assertIn(f"Context Card Title: ≤ {MAX_CONTEXT_CARD_TITLE_LENGTH} chars", prompt)
        self.assertIn(f"Bullets: ≤ {MAX_CONTEXT_CARD_BULLET_LENGTH} chars each", prompt)
        self.assertIn(f"max {MAX_CONTEXT_CARD_BULLETS_COUNT}", prompt)
        self.assertIn(f"Question Page Headline: ≤ {MAX_QUESTION_PAGE_HEADLINE_LENGTH} chars", prompt)
        self.assertIn(f"Thank You Headline: ≤ {MAX_THANK_YOU_HEADLINE_LENGTH} chars", prompt)
        self.assertIn(f"Description: ≤ {MAX_THANK_YOU_DESCRIPTION_LENGTH} chars", prompt)
        self.assertIn(f"Button Text: ≤ {MAX_CTA_BUTTON_TEXT_LENGTH} chars", prompt)
        self.assertIn(f"Privacy Policy Link Text: ≤ {MAX_PRIVACY_LINK_TEXT_LENGTH} chars", prompt)
        self.assertIn(f"max {MAX_CUSTOM_QUESTIONS_COUNT} questions", prompt)

    def test_manage_prompt_interpolates_model_constraints_and_uses_markdown_headers(self):
        """Ensure MANAGE prompt dynamically binds validation limits and uses standard markdown headers."""
        prompt = phase_prompt(Phase.MANAGE)

        # Verified standard markdown headers
        self.assertIn("### CRITICAL: FULL-REPLACEMENT FIELDS", prompt)
        self.assertIn("### PARTIAL-UPDATE (SCALAR) FIELDS", prompt)
        self.assertIn("### GENERAL CONSTRAINTS (ALL EDITS)", prompt)

        # Verified interpolated model limits
        self.assertIn(f"name                         ≤ {MAX_FORM_NAME_LENGTH} chars", prompt)
        self.assertIn(f"question_page_headline       ≤ {MAX_QUESTION_PAGE_HEADLINE_LENGTH} chars", prompt)
        self.assertIn(f"thank_you_headline           ≤ {MAX_THANK_YOU_HEADLINE_LENGTH} chars", prompt)
        self.assertIn(f"thank_you_description        ≤ {MAX_THANK_YOU_DESCRIPTION_LENGTH} chars", prompt)
        self.assertIn(f"cta_button_text              ≤ {MAX_CTA_BUTTON_TEXT_LENGTH} chars", prompt)
        self.assertIn(f"link_text (≤ {MAX_PRIVACY_LINK_TEXT_LENGTH} chars)", prompt)
        self.assertIn(f"max {MAX_CONTEXT_CARD_BULLETS_COUNT} bullets", prompt)
        self.assertIn(f"≤ {MAX_CONTEXT_CARD_BULLET_LENGTH} characters", prompt)
        self.assertIn(f"≤ {MAX_CONTEXT_CARD_TITLE_LENGTH} characters", prompt)

    def test_phase_prompt_fallback(self):
        """Ensure phase_prompt returns the STRATEGY prompt when phase is not found."""
        prompt = phase_prompt(Phase.STRATEGY)
        self.assertIn("Understand the current BusinessContext", prompt)
