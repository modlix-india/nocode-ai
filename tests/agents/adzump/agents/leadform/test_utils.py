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
    extract_privacy_url,
    format_meta_business_phone,
    format_meta_locale,
    serialize_leadform_payload,
)

FALLBACK_URL = "https://example.com"

META_ALLOWED_TOP_LEVEL_KEYS = frozenset({
    "name",
    "questions",
    "privacy_policy",
    "follow_up_action_url",
    "context_card",
    "thank_you_page",
    "question_page_custom_headline",
    "custom_disclaimer",
    "is_optimized_for_quality",
    "is_phone_sms_verify_enabled",
    "locale",
})

META_REQUIRED_TOP_LEVEL_KEYS = frozenset({
    "name",
    "questions",
    "privacy_policy",
    "follow_up_action_url",
    "context_card",
    "thank_you_page",
})


def _draft(**overrides) -> dict:
    """Returns a model_dump() of a minimal valid LeadFormRecommendation.

    Supplies a single EMAIL prefill question by default so every call site that
    does not override 'questions' still satisfies the minimum-one-question validation.
    """
    overrides.setdefault(
        "questions",
        [LeadFormQuestion(type=QuestionCategory.EMAIL)],
    )
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

    def test_no_privacy_link_returns_empty_string(self):
        product_data = {
            "site_links": [{"text": "Home", "href": "https://x.com/home"}],
            "primary_url": "https://x.com",
        }
        self.assertEqual(extract_privacy_url(product_data), "")

    def test_empty_site_links_returns_empty_string(self):
        product_data = {"site_links": [], "primary_url": "https://x.com"}
        self.assertEqual(extract_privacy_url(product_data), "")

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

    def test_privacy_link_with_empty_href_continues_to_next_valid_link(self):
        product_data = {
            "site_links": [
                {"text": "Privacy Policy", "href": ""},
                {"text": "About Us", "href": "https://x.com/about"},
                {"text": "Our Privacy Policy", "href": "https://x.com/privacy-policy"},
            ],
            "primary_url": "https://x.com",
        }
        self.assertEqual(extract_privacy_url(product_data), "https://x.com/privacy-policy")

    def test_privacy_link_with_only_empty_href_returns_empty_string(self):
        product_data = {
            "site_links": [
                {"text": "Privacy Policy", "href": ""},
                {"text": "Terms", "href": "https://x.com/terms"},
            ],
            "primary_url": "https://x.com",
        }
        self.assertEqual(extract_privacy_url(product_data), "")


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
        self.assertEqual(q["options"][0]["key"], "north")
        self.assertEqual(q["options"][1]["key"], "south")

    def test_multiple_choice_option_keys_sanitized_and_deduplicated(self):
        draft = _draft(
            questions=[
                LeadFormQuestion(
                    type=QuestionCategory.MULTIPLE_CHOICE,
                    key="budget",
                    label="What is your budget?",
                    options=[
                        "Under $50 (Best!)",
                        "50% - 75% Off",
                        "A B",
                        "A_B",
                        "a b",
                    ],
                )
            ]
        )
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        options = payload["questions"][0]["options"]
        keys = [opt["key"] for opt in options]

        # All keys must be unique
        self.assertEqual(len(keys), len(set(keys)), "Option keys must be unique within the question.")

        # Special characters must be sanitized
        self.assertEqual(keys[0], "under_50_best")
        self.assertEqual(keys[1], "50_75_off")

        # Colliding options must receive deterministic suffixes
        self.assertEqual(keys[2], "a_b")
        self.assertEqual(keys[3], "a_b_2")
        self.assertEqual(keys[4], "a_b_3")

    def test_call_business_phone_injected_in_thank_you_page(self):
        draft = _draft(
            cta_button_type=ThankYouPageButtonType.CALL_BUSINESS,
            business_phone_number="+919999999999",
        )
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertEqual(payload["thank_you_page"]["country_code"], "91")
        self.assertEqual(
            payload["thank_you_page"]["business_phone_number"], "9999999999"
        )

    def test_call_business_empty_phone_not_injected(self):
        # Build a valid draft then manually clear the phone to test serialization edge case.
        draft = LeadFormRecommendation(
            cta_button_type=ThankYouPageButtonType.CALL_BUSINESS,
            business_phone_number="+910000000000",
            questions=[LeadFormQuestion(type=QuestionCategory.EMAIL)],
        ).model_dump()
        draft["business_phone_number"] = ""
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertNotIn("business_phone_number", payload["thank_you_page"])

    def test_whatsapp_cta_injects_enable_messenger(self):
        """Meta requires enable_messenger=true for WHATSAPP button_type (H11)."""
        draft = _draft(cta_button_type=ThankYouPageButtonType.WHATSAPP)
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        ty = payload["thank_you_page"]
        self.assertEqual(ty["button_type"], "WHATSAPP")
        self.assertTrue(ty.get("enable_messenger"), "enable_messenger must be True for WHATSAPP CTA")

    def test_message_business_cta_injects_enable_messenger(self):
        """Meta requires enable_messenger=true for MESSAGE_BUSINESS button_type (H11)."""
        draft = _draft(cta_button_type=ThankYouPageButtonType.MESSAGE_BUSINESS)
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        ty = payload["thank_you_page"]
        self.assertEqual(ty["button_type"], "MESSAGE_BUSINESS")
        self.assertTrue(ty.get("enable_messenger"), "enable_messenger must be True for MESSAGE_BUSINESS CTA")

    def test_view_website_cta_does_not_inject_enable_messenger(self):
        """enable_messenger must NOT appear for non-messenger CTA types."""
        draft = _draft(cta_button_type=ThankYouPageButtonType.VIEW_WEBSITE)
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertNotIn("enable_messenger", payload["thank_you_page"])

    def test_call_business_cta_does_not_inject_enable_messenger(self):
        """enable_messenger must NOT appear for CALL_BUSINESS — no cross-contamination."""
        draft = _draft(
            cta_button_type=ThankYouPageButtonType.CALL_BUSINESS,
            business_phone_number="+919999999999",
        )
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertNotIn("enable_messenger", payload["thank_you_page"])

    def test_cover_photo_id_injected_when_set(self):
        form = LeadFormRecommendation(
            questions=[LeadFormQuestion(type=QuestionCategory.EMAIL)]
        )
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

    def test_custom_disclaimer_uses_custom_title_when_provided(self):
        draft = _draft(
            custom_disclaimer="Special legal text.",
            custom_disclaimer_title="Terms & Conditions",
        )
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertIn("custom_disclaimer", payload)
        self.assertEqual(payload["custom_disclaimer"]["title"], "Terms & Conditions")
        self.assertEqual(
            payload["custom_disclaimer"]["body"]["text"], "Special legal text."
        )

    def test_custom_disclaimer_falls_back_to_default_title_when_empty_or_whitespace(self):
        draft = _draft(
            custom_disclaimer="Special legal text.",
            custom_disclaimer_title="   ",
        )
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertEqual(payload["custom_disclaimer"]["title"], "Disclaimer")

    def test_form_name_propagated_to_payload(self):
        draft = _draft(name="My Lead Form 2026")
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertEqual(payload["name"], "My Lead Form 2026")

    def test_no_custom_disclaimer_key_absent(self):
        draft = _draft(custom_disclaimer="")
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertNotIn("custom_disclaimer", payload)

    def test_privacy_url_remains_empty_when_not_provided(self):
        draft = _draft()
        draft["privacy_policy"]["url"] = ""
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertEqual(payload["privacy_policy"]["url"], "")

    def test_list_style_context_card_preserved(self):
        form = LeadFormRecommendation(
            context_card=ContextCard(
                style=ContextCardStyle.LIST_STYLE,
                content=["Point one."],
            ),
            questions=[LeadFormQuestion(type=QuestionCategory.EMAIL)],
        )
        payload = serialize_leadform_payload(form.model_dump(), FALLBACK_URL)
        self.assertEqual(payload["context_card"]["style"], "LIST_STYLE")

    def test_invalid_style_coerced_to_paragraph(self):
        draft = _draft()
        draft["context_card"]["style"] = "INVALID_STYLE"
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertEqual(payload["context_card"]["style"], "PARAGRAPH_STYLE")

    def test_follow_up_action_url_is_top_level_uri_string(self):
        draft = _draft()
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertIn("follow_up_action_url", payload)
        self.assertEqual(payload["follow_up_action_url"], FALLBACK_URL)
        self.assertNotIn("follow_up_action", payload)

    def test_question_page_custom_headline_omitted_when_empty(self):
        draft = _draft()
        draft["question_page_headline"] = ""
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertNotIn("question_page_custom_headline", payload)

        draft["question_page_headline"] = "   "
        payload_blank = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertNotIn("question_page_custom_headline", payload_blank)

    def test_question_page_custom_headline_included_when_set(self):
        draft = _draft()
        draft["question_page_headline"] = "Get Your Free Consultation"
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertIn("question_page_custom_headline", payload)
        self.assertEqual(payload["question_page_custom_headline"], "Get Your Free Consultation")

    def test_locale_omitted_when_not_provided(self):
        draft = _draft()
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertNotIn("locale", payload)

    def test_locale_included_from_draft(self):
        draft = _draft(locale="es_la")
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertEqual(payload.get("locale"), "ES_LA")

    def test_locale_included_from_argument(self):
        draft = _draft()
        payload = serialize_leadform_payload(draft, FALLBACK_URL, locale="FR")
        self.assertEqual(payload.get("locale"), "FR_FR")

    def test_minimal_payload_has_exact_required_top_level_keys(self):
        draft = _draft()
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertEqual(set(payload.keys()), META_REQUIRED_TOP_LEVEL_KEYS)

    def test_all_emitted_keys_conform_to_meta_schema(self):
        draft = _draft(
            questions=[LeadFormQuestion(type=QuestionCategory.PHONE)],
            question_page_headline="Custom Headline",
            custom_disclaimer="Disclaimer text",
            is_higher_intent=True,
            is_phone_sms_verify_enabled=True,
            locale="EN_US",
        )
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertEqual(set(payload.keys()), META_ALLOWED_TOP_LEVEL_KEYS)
        self.assertTrue(set(payload.keys()).issubset(META_ALLOWED_TOP_LEVEL_KEYS))

    def test_phone_sms_verify_flag_set_when_true(self):
        draft = _draft(
            questions=[LeadFormQuestion(type=QuestionCategory.PHONE)],
            is_phone_sms_verify_enabled=True,
        )
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertIn("is_phone_sms_verify_enabled", payload)
        self.assertTrue(payload["is_phone_sms_verify_enabled"])

    def test_phone_sms_verify_flag_absent_when_false(self):
        draft = _draft(is_phone_sms_verify_enabled=False)
        payload = serialize_leadform_payload(draft, FALLBACK_URL)
        self.assertNotIn("is_phone_sms_verify_enabled", payload)


class TestFormatMetaLocale(unittest.TestCase):
    def test_country_code_mapping(self):
        self.assertEqual(format_meta_locale("US"), "EN_US")
        self.assertEqual(format_meta_locale("IN"), "EN_US")
        self.assertEqual(format_meta_locale("GB"), "EN_GB")
        self.assertEqual(format_meta_locale("DE"), "DE_DE")
        self.assertEqual(format_meta_locale("FR"), "FR_FR")
        self.assertEqual(format_meta_locale("ES"), "ES_ES")
        self.assertEqual(format_meta_locale("BR"), "PT_BR")
        self.assertEqual(format_meta_locale("MX"), "ES_LA")

    def test_iso_locale_normalization(self):
        self.assertEqual(format_meta_locale("en_us"), "EN_US")
        self.assertEqual(format_meta_locale("en-us"), "EN_US")
        self.assertEqual(format_meta_locale("es_la"), "ES_LA")
        self.assertEqual(format_meta_locale("fr_fr"), "FR_FR")

    def test_language_fallback(self):
        self.assertEqual(format_meta_locale("es"), "ES_ES")
        self.assertEqual(format_meta_locale("fr"), "FR_FR")
        self.assertEqual(format_meta_locale("de"), "DE_DE")

    def test_empty_or_invalid_returns_empty(self):
        self.assertEqual(format_meta_locale(""), "")
        self.assertEqual(format_meta_locale(None), "")
        self.assertEqual(format_meta_locale("   "), "")
        self.assertEqual(format_meta_locale("unknown_long_invalid_string"), "")



class TestFormatMetaBusinessPhone(unittest.TestCase):
    def test_indian_e164_format(self):
        cc, num = format_meta_business_phone("+919876543210")
        self.assertEqual(cc, "91")
        self.assertEqual(num, "9876543210")

    def test_indian_12_digits_without_plus(self):
        cc, num = format_meta_business_phone("919876543210")
        self.assertEqual(cc, "91")
        self.assertEqual(num, "9876543210")

    def test_indian_leading_zero(self):
        cc, num = format_meta_business_phone("09876543210")
        self.assertEqual(cc, "91")
        self.assertEqual(num, "9876543210")

    def test_indian_raw_10_digits(self):
        cc, num = format_meta_business_phone("9876543210")
        self.assertEqual(cc, "91")
        self.assertEqual(num, "9876543210")

    def test_indian_with_spaces_and_dashes(self):
        cc, num = format_meta_business_phone("+91 98765-43210")
        self.assertEqual(cc, "91")
        self.assertEqual(num, "9876543210")

    def test_empty_or_none(self):
        self.assertEqual(format_meta_business_phone(""), ("", ""))
        self.assertEqual(format_meta_business_phone("   "), ("", ""))
        self.assertEqual(format_meta_business_phone(None), ("", ""))
        self.assertEqual(format_meta_business_phone(None), ("", ""))


if __name__ == "__main__":
    unittest.main()

