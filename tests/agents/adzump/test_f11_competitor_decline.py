"""F11 · competitor-analysis decline capture (below the model).

The decline-detection was a brittle exact-match (`"no" in lu.split()`) that broke
on the comma in "No, skip competitor analysis for now" → the question re-asked.
Fixed with the shared substring helper `is_decline` (mirrors `is_ig_skip`), used
by the `_field_traceable` guard. Polarity-flips ("no, change the budget") must NOT
read as a decline. Pure functions → no model, no agent."""
import unittest

from app.agents.adzump.tools.campaign_data import _field_traceable, is_decline

DECLINES = [
    "No, skip competitor analysis for now",  # the live F11 phrase (comma!)
    "no", "n", "No", "skip", "skip it", "not now", "no need", "no thanks",
    "maybe later", "don't bother",
]
NOT_DECLINES = [  # polarity-flips: 'no' rejects something ELSE, not the offer
    "no, change the budget to 20k",
    "no, that competitor is wrong",
    "yes", "go ahead, analyze them", "analyze competitors", "",
]


class IsDeclineTests(unittest.TestCase):
    def test_declines_true(self):
        for t in DECLINES:
            self.assertTrue(is_decline(t), f"expected decline: {t!r}")

    def test_polarity_flips_false(self):
        for t in NOT_DECLINES:
            self.assertFalse(is_decline(t), f"expected NOT a decline: {t!r}")


class FieldTraceableTests(unittest.TestCase):
    def test_typed_decline_traces(self):  # the F11 fix
        self.assertTrue(_field_traceable(
            "competitive_analysis_declined", "true", "No, skip competitor analysis for now", {}))

    def test_chip_decline_traces(self):  # chip "No" carries answer="true" — must still pass
        self.assertTrue(_field_traceable("competitive_analysis_declined", "true", "No", {}))

    def test_polarity_flip_rejected(self):
        self.assertFalse(_field_traceable(
            "competitive_analysis_declined", "true", "no, change the budget to 20k", {}))

    def test_non_true_value_rejected(self):
        self.assertFalse(_field_traceable(
            "competitive_analysis_declined", "false", "no", {}))


if __name__ == "__main__":
    unittest.main()
