"""Lead Form utils — serialization and extraction contract tests.

Pins the exact JSON shape that serialize_leadform_payload produces for the
Meta Graph API. Any mismatch in field names, nesting, or presence/absence of
optional keys will cause a 400 error in production.
"""
import unittest

from app.agents.adzump.agents.leadform.models import (
    ContextCard,
    ContextCardStyle,
    LeadFormQuestion,
    LeadFormRecommendation,
    QuestionCategory,
    ThankYouPageButtonType,
)
from app.agents.adzump.agents.leadform.utils import (
    build_business_context,
    extract_privacy_url,
    serialize_leadform_payload,
)

FALLBACK_URL = "https://example.com"


def _draft(**overrides) -> dict:
    """Returns a model_dump() of a minimal valid LeadFormRecommendation."""
    return LeadFormRecommendation(**overrides).model_dump()


class ExtractPrivacyUrlTests(unittest.TestCase):
    def test_privacy_link_found_in_dict_site_links(self):
        product_data = {
            "site_links": [
                {"text": "About Us", "href": "https://x.com/about"},
                {"text": "Privacy Policy", "href": "https://x.com/privacy"},
            ],
            "primary_url": "https://x.com",
        }
        self.assertEqual(extract_privacy_url(product_data), "https://x.com/privacy")

    def test_privacy_link_case_insensitive(self):
        product_data = {
            "site_links": [{"text": "PRIVACY", "href": "https://x.com/priv"}],
            "primary_url": "https://x.com",
        }
        self.assertEqual(extract_privacy_url(product_data), "https://x.com/priv")

    def test_no_privacy_link_falls_back_to_primary_url(self):
        product_data = {
            "site_links": [{"text": "Home", "href": "https://x.com/home"}],
            "primary_url": "https://x.com",
        }
        self.assertEqual(extract_privacy_url(product_data), "https://x.com")

    def test_empty_site_links_falls_back_to_primary_url(self):
        product_data = {"site_links": [], "primary_url": "https://x.com"}
        self.assertEqual(extract_privacy_url(product_data), "https://x.com")

    def test_privacy_link_found_on_object_attrs(self):
        class _Link:
            def __init__(self, text, href):
                self.text = text
                self.href = href

        product_data = {
            "site_links": [_Link("Privacy Policy", "https://x.com/privacy")],
            "primary_url": "https://x.com",
        }
        self.assertEqual(extract_privacy_url(product_data), "https://x.com/privacy")


class SerializeLeadformPayloadTests(unittest.TestCase):
    def test_prefill_question_type_preserved(self):
        draft = _draft(questions=[LeadFormQuestion(type=QuestionCategory.EMAIL)])
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertEqual(payload["questions"][0]["type"], "EMAIL")

    def test_custom_short_answer_mapped_to_custom_type(self):
        draft = _draft(
            questions=[
                LeadFormQuestion(
                    type=QuestionCategory.SHORT_ANSWER,
                    key="budget",
                    label="What is your budget?",
                )
            ]
        )
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        q = payload["questions"][0]
        self.assertEqual(q["type"], "CUSTOM")
        self.assertEqual(q["label"], "What is your budget?")
        self.assertEqual(q["key"], "budget")
        self.assertNotIn("options", q)

    def test_multiple_choice_options_serialized_with_value_and_key(self):
        draft = _draft(
            questions=[
                LeadFormQuestion(
                    type=QuestionCategory.MULTIPLE_CHOICE,
                    key="area",
                    label="Preferred area?",
                    options=["North", "South"],
                )
            ]
        )
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        q = payload["questions"][0]
        self.assertEqual(q["type"], "CUSTOM")
        self.assertEqual(len(q["options"]), 2)
        self.assertIn("value", q["options"][0])
        self.assertIn("key", q["options"][0])

    def test_call_business_phone_injected_in_thank_you_page(self):
        draft = _draft(
            cta_button_type=ThankYouPageButtonType.CALL_BUSINESS,
            business_phone_number="+919999999999",
        )
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertEqual(
            payload["thank_you_page"]["business_phone_number"], "+919999999999"
        )

    def test_call_business_empty_phone_not_injected(self):
        # Build a valid draft then manually clear the phone to test serialization edge case.
        draft = LeadFormRecommendation(
            cta_button_type=ThankYouPageButtonType.CALL_BUSINESS,
            business_phone_number="+910000000000",
        ).model_dump()
        draft["business_phone_number"] = ""
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertNotIn("business_phone_number", payload["thank_you_page"])

    def test_cover_photo_id_injected_when_set(self):
        form = LeadFormRecommendation()
        form.context_card.cover_photo_id = "photo_123"
        payload = serialize_leadform_payload(form.model_dump(), FALLBACK_URL)
        self.assertEqual(payload["context_card"]["cover_photo_id"], "photo_123")

    def test_cover_photo_id_absent_when_empty(self):
        draft = _draft()
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertNotIn("cover_photo_id", payload["context_card"])

    def test_higher_intent_flag_set_when_true(self):
        draft = _draft(is_higher_intent=True)
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertTrue(payload.get("is_optimized_for_quality"))

    def test_higher_intent_flag_absent_when_false(self):
        draft = _draft(is_higher_intent=False)
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertNotIn("is_optimized_for_quality", payload)

    def test_custom_disclaimer_has_correct_shape(self):
        draft = _draft(custom_disclaimer="This is a legal disclaimer.")
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertIn("custom_disclaimer", payload)
        self.assertEqual(payload["custom_disclaimer"]["title"], "Disclaimer")
        self.assertEqual(
            payload["custom_disclaimer"]["body"]["text"], "This is a legal disclaimer."
        )

    def test_no_custom_disclaimer_key_absent(self):
        draft = _draft(custom_disclaimer="")
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertNotIn("custom_disclaimer", payload)

    def test_privacy_url_falls_back_to_website_url_when_empty(self):
        draft = _draft()
        draft["privacy_policy"]["url"] = ""
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertEqual(payload["privacy_policy"]["url"], FALLBACK_URL)

    def test_list_style_context_card_preserved(self):
        form = LeadFormRecommendation(
            context_card=ContextCard(
                style=ContextCardStyle.LIST_STYLE,
                content=["Point one."],
            )
        )
        payload = serialize_leadform_payload(form.model_dump(), FALLBACK_URL)
        self.assertEqual(payload["context_card"]["style"], "LIST_STYLE")

    def test_invalid_style_coerced_to_paragraph(self):
        draft = _draft()
        draft["context_card"]["style"] = "INVALID_STYLE"
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertEqual(payload["context_card"]["style"], "PARAGRAPH_STYLE")


if __name__ == "__main__":
    unittest.main()
