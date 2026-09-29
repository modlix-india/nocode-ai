"""Lead Form models — Pydantic validation contract tests.

Pins the character limits, bullet count rules, question key constraints,
and CTA-specific requirements enforced by LeadFormRecommendation.validate_lead_form
so they cannot regress silently.
"""
import unittest

import pydantic

from app.agents.adzump.agents.leadform.models import (
    MAX_CONTEXT_CARD_BULLET_LENGTH,
    MAX_CONTEXT_CARD_BULLETS_COUNT,
    MAX_CONTEXT_CARD_TITLE_LENGTH,
    MAX_CUSTOM_QUESTIONS_COUNT,
    MAX_QUESTION_KEY_LENGTH,
    MAX_QUESTION_PAGE_HEADLINE_LENGTH,
    QUESTION_SCHEMA_PARAM,
    ContextCard,
    ContextCardStyle,
    LeadFormQuestion,
    LeadFormRecommendation,
    QuestionCategory,
    ThankYouPageButtonType,
)


def _minimal_form(**overrides) -> LeadFormRecommendation:
    """Returns the simplest valid LeadFormRecommendation with optional field overrides.

    Supplies a single EMAIL prefill question by default so every call site that
    does not override 'questions' still satisfies the H2 minimum-one-question guard.
    Callers that pass their own 'questions' list override this default.
    """
    overrides.setdefault(
        "questions",
        [LeadFormQuestion(type=QuestionCategory.EMAIL)],
    )
    return LeadFormRecommendation(**overrides)


class LeadFormQuestionAutoKeyTests(unittest.TestCase):
    def test_key_auto_derived_from_label(self):
        q = LeadFormQuestion(type=QuestionCategory.SHORT_ANSWER, label="What is your budget?")
        self.assertEqual(q.key, "what_is_your_budget")

    def test_key_max_length_truncated(self):
        long_label = "This is a very long label that definitely exceeds thirty characters"
        q = LeadFormQuestion(type=QuestionCategory.SHORT_ANSWER, label=long_label)
        self.assertLessEqual(len(q.key), MAX_QUESTION_KEY_LENGTH)

    def test_key_special_chars_stripped(self):
        import re
        q = LeadFormQuestion(type=QuestionCategory.SHORT_ANSWER, label="1 Cr Rs budget option?")
        self.assertRegex(q.key, r"^[a-z0-9_]+$")

    def test_key_preserved_if_explicitly_set(self):
        q = LeadFormQuestion(type=QuestionCategory.SHORT_ANSWER, key="my_key", label="Anything")
        self.assertEqual(q.key, "my_key")

    def test_prefill_type_does_not_auto_generate_key(self):
        q = LeadFormQuestion(type=QuestionCategory.EMAIL)
        self.assertEqual(q.key, "")

    def test_marital_status_wire_value_is_maritial_status(self):
        q1 = LeadFormQuestion(type=QuestionCategory.MARITAL_STATUS)
        self.assertEqual(q1.type.value, "MARITIAL_STATUS")
        q2 = LeadFormQuestion(type="MARITAL_STATUS")
        self.assertEqual(q2.type.value, "MARITIAL_STATUS")

    def test_high_value_types_date_time_and_store_lookup(self):
        q_dt = LeadFormQuestion(type=QuestionCategory.DATE_TIME)
        self.assertEqual(q_dt.type.value, "DATE_TIME")
        q_sl = LeadFormQuestion(type=QuestionCategory.STORE_LOOKUP)
        self.assertEqual(q_sl.type.value, "STORE_LOOKUP")

    def test_question_schema_param_declares_all_enums(self):
        enum_list = QUESTION_SCHEMA_PARAM["items"]["properties"]["type"]["enum"]
        self.assertIn("MARITIAL_STATUS", enum_list)
        self.assertIn("DATE_TIME", enum_list)
        self.assertIn("STORE_LOOKUP", enum_list)
        self.assertNotIn("NATIONAL_ID_NUMBER", enum_list)
        self.assertEqual(len(enum_list), len(QuestionCategory))


class ContextCardValidationTests(unittest.TestCase):
    def test_title_at_limit_passes(self):
        ContextCard(title="a" * MAX_CONTEXT_CARD_TITLE_LENGTH)

    def test_title_over_limit_raises(self):
        with self.assertRaises(pydantic.ValidationError):
            ContextCard(title="a" * (MAX_CONTEXT_CARD_TITLE_LENGTH + 1))

    def test_list_style_max_bullets_passes(self):
        _minimal_form(
            context_card=ContextCard(
                style=ContextCardStyle.LIST_STYLE,
                content=["bullet"] * MAX_CONTEXT_CARD_BULLETS_COUNT,
            )
        )

    def test_list_style_exceeds_max_bullets_raises(self):
        with self.assertRaises(pydantic.ValidationError):
            _minimal_form(
                context_card=ContextCard(
                    style=ContextCardStyle.LIST_STYLE,
                    content=["bullet"] * (MAX_CONTEXT_CARD_BULLETS_COUNT + 1),
                )
            )

    def test_paragraph_style_single_item_passes(self):
        _minimal_form(
            context_card=ContextCard(
                style=ContextCardStyle.PARAGRAPH_STYLE,
                content=["One paragraph."],
            )
        )

    def test_paragraph_style_two_items_raises(self):
        with self.assertRaises(pydantic.ValidationError):
            _minimal_form(
                context_card=ContextCard(
                    style=ContextCardStyle.PARAGRAPH_STYLE,
                    content=["First paragraph.", "Second paragraph."],
                )
            )

    def test_bullet_over_80_chars_raises(self):
        with self.assertRaises(pydantic.ValidationError):
            _minimal_form(
                context_card=ContextCard(
                    style=ContextCardStyle.LIST_STYLE,
                    content=["a" * (MAX_CONTEXT_CARD_BULLET_LENGTH + 1)],
                )
            )

    def test_bullet_at_limit_passes(self):
        _minimal_form(
            context_card=ContextCard(
                style=ContextCardStyle.LIST_STYLE,
                content=["a" * MAX_CONTEXT_CARD_BULLET_LENGTH],
            )
        )


class LeadFormRecommendationValidationTests(unittest.TestCase):
    def test_valid_minimal_form_passes(self):
        form = _minimal_form()
        self.assertIsInstance(form, LeadFormRecommendation)

    # ── H2: minimum-one-question guard ───────────────────────────────────────

    def test_zero_questions_raises(self):
        """H2: A form with no questions must be rejected at draft-validation time,
        before it can reach the irreversible publish_to_meta call."""
        with self.assertRaises(pydantic.ValidationError) as ctx:
            LeadFormRecommendation(questions=[])
        self.assertIn("at least one question", str(ctx.exception))

    def test_single_prefill_question_passes(self):
        """H2: A form with exactly one prefill question must be accepted."""
        form = LeadFormRecommendation(
            questions=[LeadFormQuestion(type=QuestionCategory.EMAIL)]
        )
        self.assertEqual(len(form.questions), 1)

    def test_question_page_headline_over_limit_raises(self):
        with self.assertRaises(pydantic.ValidationError):
            _minimal_form(question_page_headline="a" * (MAX_QUESTION_PAGE_HEADLINE_LENGTH + 1))

    def test_question_page_headline_at_limit_passes(self):
        _minimal_form(question_page_headline="a" * MAX_QUESTION_PAGE_HEADLINE_LENGTH)

    def test_max_custom_questions_passes(self):
        questions = [
            LeadFormQuestion(
                type=QuestionCategory.SHORT_ANSWER,
                key=f"q_{i}",
                label=f"Question {i}",
            )
            for i in range(MAX_CUSTOM_QUESTIONS_COUNT)
        ]
        _minimal_form(questions=questions)

    def test_exceeds_max_custom_questions_raises(self):
        questions = [
            LeadFormQuestion(
                type=QuestionCategory.SHORT_ANSWER,
                key=f"q_{i}",
                label=f"Question {i}",
            )
            for i in range(MAX_CUSTOM_QUESTIONS_COUNT + 1)
        ]
        with self.assertRaises(pydantic.ValidationError):
            _minimal_form(questions=questions)

    def test_duplicate_question_key_raises(self):
        questions = [
            LeadFormQuestion(type=QuestionCategory.SHORT_ANSWER, key="same_key", label="Question A"),
            LeadFormQuestion(type=QuestionCategory.SHORT_ANSWER, key="same_key", label="Question B"),
        ]
        with self.assertRaises(pydantic.ValidationError):
            _minimal_form(questions=questions)

    def test_custom_question_empty_key_and_label_raises(self):
        # auto_key only fires when label is truthy; empty key + empty label stays empty → error.
        questions = [
            LeadFormQuestion(type=QuestionCategory.SHORT_ANSWER, key="", label=""),
        ]
        with self.assertRaises(pydantic.ValidationError):
            _minimal_form(questions=questions)

    def test_multiple_choice_one_option_raises(self):
        questions = [
            LeadFormQuestion(
                type=QuestionCategory.MULTIPLE_CHOICE,
                key="mc_q",
                label="Pick one",
                options=["Only option"],
            )
        ]
        with self.assertRaises(pydantic.ValidationError):
            _minimal_form(questions=questions)

    def test_multiple_choice_two_options_passes(self):
        questions = [
            LeadFormQuestion(
                type=QuestionCategory.MULTIPLE_CHOICE,
                key="mc_q",
                label="Pick one",
                options=["Option A", "Option B"],
            )
        ]
        _minimal_form(questions=questions)

    # ── CALL_BUSINESS CTA validation ─────────────────────────────────────────

    def test_call_business_no_phone_raises(self):
        with self.assertRaises(pydantic.ValidationError):
            _minimal_form(
                cta_button_type=ThankYouPageButtonType.CALL_BUSINESS,
                business_phone_number="",
            )

    def test_call_business_whitespace_only_phone_raises(self):
        with self.assertRaises(pydantic.ValidationError):
            _minimal_form(
                cta_button_type=ThankYouPageButtonType.CALL_BUSINESS,
                business_phone_number="   ",
            )

    def test_call_business_with_valid_phone_passes(self):
        form = _minimal_form(
            cta_button_type=ThankYouPageButtonType.CALL_BUSINESS,
            business_phone_number="+919999999999",
        )
        self.assertEqual(form.cta_button_type, ThankYouPageButtonType.CALL_BUSINESS)
        self.assertEqual(form.business_phone_number, "+919999999999")

    def test_view_website_no_phone_passes(self):
        _minimal_form(
            cta_button_type=ThankYouPageButtonType.VIEW_WEBSITE,
            business_phone_number="",
        )

    def test_whatsapp_no_phone_passes(self):
        _minimal_form(
            cta_button_type=ThankYouPageButtonType.WHATSAPP,
            business_phone_number="",
        )

    def test_download_no_phone_passes(self):
        _minimal_form(
            cta_button_type=ThankYouPageButtonType.DOWNLOAD,
            business_phone_number="",
        )

    # ── is_phone_sms_verify_enabled validation ───────────────────────────────

    def test_phone_sms_verify_with_phone_passes(self):
        form = _minimal_form(
            is_phone_sms_verify_enabled=True,
            questions=[LeadFormQuestion(type=QuestionCategory.PHONE, label="Phone")],
        )
        self.assertTrue(form.is_phone_sms_verify_enabled)

    def test_phone_sms_verify_with_phone_otp_passes(self):
        form = _minimal_form(
            is_phone_sms_verify_enabled=True,
            questions=[LeadFormQuestion(type=QuestionCategory.PHONE_OTP, label="Phone OTP")],
        )
        self.assertTrue(form.is_phone_sms_verify_enabled)

    def test_phone_sms_verify_without_phone_raises(self):
        with self.assertRaises(pydantic.ValidationError):
            _minimal_form(
                is_phone_sms_verify_enabled=True,
                questions=[LeadFormQuestion(type=QuestionCategory.EMAIL, label="Email")],
            )


if __name__ == "__main__":
    unittest.main()

