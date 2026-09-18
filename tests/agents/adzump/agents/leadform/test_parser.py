"""Lead Form parser — Meta Graph API response normalization tests.

Tests the fail-soft iteration, type coercion, and structural mapping of
raw Meta leadgen_forms API responses into LeadFormProfile objects.
If Meta changes a field name, these tests surface the breakage immediately.
"""
import unittest

from app.agents.adzump.agents.leadform.models import QuestionCategory
from app.agents.adzump.agents.leadform.parser import _parse_question, parse_leadgen_forms


def _raw_form(**overrides) -> dict:
    """Returns a minimal valid raw Meta lead form dict."""
    base = {
        "id": "123456789",
        "name": "Test Form",
        "status": "ACTIVE",
        "leads_count": 10,
    }
    base.update(overrides)
    return base


class ParseQuestionTests(unittest.TestCase):
    def test_standard_prefill_email_parsed(self):
        q = _parse_question({"type": "EMAIL"})
        self.assertEqual(q.type, QuestionCategory.EMAIL)

    def test_standard_prefill_type_case_insensitive(self):
        q = _parse_question({"type": "email"})
        self.assertEqual(q.type, QuestionCategory.EMAIL)

    def test_standard_prefill_full_name(self):
        q = _parse_question({"type": "FULL_NAME"})
        self.assertEqual(q.type, QuestionCategory.FULL_NAME)

    def test_custom_short_answer_no_options(self):
        q = _parse_question({"type": "CUSTOM", "label": "What is your budget?", "key": "budget"})
        self.assertEqual(q.type, QuestionCategory.SHORT_ANSWER)
        self.assertEqual(q.label, "What is your budget?")
        self.assertEqual(len(q.options), 0)

    def test_custom_multiple_choice_with_dict_options(self):
        q = _parse_question({
            "type": "CUSTOM",
            "key": "city",
            "label": "City?",
            "options": [{"value": "Mumbai"}, {"value": "Delhi"}],
        })
        self.assertEqual(q.type, QuestionCategory.MULTIPLE_CHOICE)
        self.assertEqual(q.options, ["Mumbai", "Delhi"])

    def test_custom_multiple_choice_with_string_options(self):
        q = _parse_question({
            "type": "CUSTOM",
            "key": "city",
            "label": "City?",
            "options": ["Mumbai", "Delhi"],
        })
        self.assertEqual(q.type, QuestionCategory.MULTIPLE_CHOICE)
        self.assertEqual(q.options, ["Mumbai", "Delhi"])

    def test_unrecognized_type_mapped_to_short_answer(self):
        q = _parse_question({"type": "UNKNOWN_FUTURE_META_TYPE", "key": "x", "label": "X"})
        self.assertEqual(q.type, QuestionCategory.SHORT_ANSWER)

    def test_empty_type_mapped_to_short_answer(self):
        q = _parse_question({"type": ""})
        self.assertEqual(q.type, QuestionCategory.SHORT_ANSWER)


class ParseLeadgenFormsTests(unittest.TestCase):
    def test_empty_list_returns_empty(self):
        self.assertEqual(parse_leadgen_forms([]), [])

    def test_valid_form_fields_mapped_correctly(self):
        profiles = parse_leadgen_forms([_raw_form()])
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].id, "123456789")
        self.assertEqual(profiles[0].name, "Test Form")
        self.assertEqual(profiles[0].status, "ACTIVE")
        self.assertEqual(profiles[0].leads_count, 10)

    def test_form_without_id_is_skipped(self):
        profiles = parse_leadgen_forms([{"name": "No ID Form"}])
        self.assertEqual(profiles, [])

    def test_malformed_form_skipped_valid_form_survives(self):
        # leads_count as non-numeric string forces int() to raise ValueError → skipped.
        bad_form = {"id": "bad", "leads_count": "not_an_int"}
        good_form = _raw_form(id="999")
        profiles = parse_leadgen_forms([bad_form, good_form])
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].id, "999")

    def test_higher_intent_flag_true_when_optimized(self):
        profiles = parse_leadgen_forms([_raw_form(is_optimized_for_quality=True)])
        self.assertTrue(profiles[0].is_higher_intent)

    def test_higher_intent_false_when_flag_absent(self):
        profiles = parse_leadgen_forms([_raw_form()])
        self.assertFalse(profiles[0].is_higher_intent)

    def test_leads_count_cast_from_string_to_int(self):
        profiles = parse_leadgen_forms([_raw_form(leads_count="42")])
        self.assertEqual(profiles[0].leads_count, 42)

    def test_leads_count_defaults_to_zero_when_missing(self):
        raw = {k: v for k, v in _raw_form().items() if k != "leads_count"}
        profiles = parse_leadgen_forms([raw])
        self.assertEqual(profiles[0].leads_count, 0)

    def test_questions_parsed_within_form(self):
        raw = _raw_form(questions=[{"type": "EMAIL"}, {"type": "PHONE"}])
        profiles = parse_leadgen_forms([raw])
        self.assertEqual(len(profiles[0].questions), 2)
        self.assertEqual(profiles[0].questions[0].type, QuestionCategory.EMAIL)
        self.assertEqual(profiles[0].questions[1].type, QuestionCategory.PHONE)

    def test_non_dict_question_entries_skipped(self):
        raw = _raw_form(questions=["not_a_dict", {"type": "EMAIL"}])
        profiles = parse_leadgen_forms([raw])
        # Only the dict entry should be parsed; the string is silently skipped.
        self.assertEqual(len(profiles[0].questions), 1)
        self.assertEqual(profiles[0].questions[0].type, QuestionCategory.EMAIL)


if __name__ == "__main__":
    unittest.main()
