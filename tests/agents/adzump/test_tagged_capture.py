"""PR2 · tagged-answer capture — correctness guards (real guard, no mocks).

Covers the properties with no other coverage:
  1. _capture_tagged_answer stores the right value, pops the flag on success,
     and falls through (no store, flag intact) for Custom / Yes / off-topic /
     unparseable — for chips AND typed free-text (D12).
  2. The capture and LLM paths share ONE guard: _field_traceable is
     normalization-aware for duration/budget (parse both sides, compare
     canonical), so "4k" → "₹4,000/day" is accepted.
  3. Resume safety: gated on the agentic-loop turn, not session._turn_count.
  4. Every data-ask present_options prescription in _next_action carries field=
     (D9 audit), and the audit has teeth (negative test).

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump.test_tagged_capture -v
"""
from __future__ import annotations

import asyncio
import types
import unittest

from app.agents.adzump.agent import AdzumpAgent, _next_action, CampaignContext
from app.agents.adzump.answer_parse import parse_typed_answer
from app.agents.adzump.tools.campaign_data import _field_traceable, _apply_field
from app.agents.adzump.tools.suggestions import _present_options

RE = {"business_type": "real estate"}


def _session(pe, user, *, spec=None, turn=1, product=RE):
    s = types.SimpleNamespace()
    s.context = {
        "_pending_elicitation": dict(pe) if pe else None,
        "campaign_spec": dict(spec or {}),
        "_spec_set_at": {},
        "product_data": dict(product),
    }
    s.messages = [{"role": "user", "content": user}]
    s._turn_count = turn
    return s


def _cap(s, turn=1):
    return AdzumpAgent._capture_tagged_answer(None, s, turn=turn)


def _decline_pe(answers=None):
    return {"tool": "present_options", "expects": "single",
            "field": "competitive_analysis_declined", "answers": answers or {"No": "true"}}


def _dur_pe():
    return {"tool": "present_options", "expects": "single",
            "field": "duration", "answers": {"30 days": "30 days", "60 days": "60 days"}}


class CaptureChipTests(unittest.TestCase):
    def test_competitor_no_stores_true_and_pops(self):
        s = _session(_decline_pe(), "No")
        ack = _cap(s)
        self.assertEqual(s.context["campaign_spec"].get("competitive_analysis_declined"), "true")
        self.assertNotIn("_pending_elicitation", s.context)   # consumed
        self.assertTrue(ack)                                  # acknowledgement steer (D14)

    def test_competitor_yes_falls_through(self):
        s = _session(_decline_pe(), "Yes")                    # "Yes" not in answers map
        self.assertEqual(_cap(s), "")
        self.assertEqual(s.context["campaign_spec"], {})
        self.assertIsNotNone(s.context["_pending_elicitation"])  # intact → LLM runs analyze_competitors

    def test_duration_chip_stores_and_provenance(self):
        s = _session(_dur_pe(), "30 days")
        _cap(s)
        self.assertEqual(s.context["campaign_spec"].get("duration"), "30 days")
        self.assertEqual(s.context["_spec_set_at"].get("duration"), 1)

    def test_custom_triggers_freetext_steer(self):
        # v4 · F10 changed this: "Custom" (no answer) no longer falls through
        # silently — it keeps the elicitation OPEN, marks awaiting_custom, and
        # returns a free-text steer so the typed value is captured next turn
        # (instead of re-rendering the same chips, live bug #11). Still not stored.
        s = _session(_dur_pe(), "Custom")
        ack = _cap(s)
        self.assertIn("custom value", ack.lower())            # F10 free-text steer
        self.assertEqual(s.context["campaign_spec"], {})      # "Custom" is not a value
        self.assertIsNotNone(s.context["_pending_elicitation"])           # kept open
        self.assertTrue(s.context["_pending_elicitation"].get("awaiting_custom"))


class CaptureTypedTests(unittest.TestCase):
    def test_typed_duration(self):
        s = _session(_dur_pe(), "25 days")                    # no exact match → parser
        _cap(s)
        self.assertEqual(s.context["campaign_spec"].get("duration"), "25 days")

    def test_typed_budget_4k_real_estate(self):
        s = _session({"tool": "present_options", "expects": "single", "field": "budget", "answers": {}}, "4k")
        _cap(s)
        self.assertEqual(s.context["campaign_spec"].get("budget"), "₹4,000/day")

    def test_typed_budget_no_marker_falls_through(self):
        s = _session({"tool": "present_options", "expects": "single", "field": "budget", "answers": {}}, "around 4000")
        self.assertEqual(_cap(s), "")
        self.assertEqual(s.context["campaign_spec"], {})

    def test_cross_field_correction_falls_through(self):
        s = _session(_dur_pe(), "make it Meta")               # not a duration → no store
        self.assertEqual(_cap(s), "")
        self.assertEqual(s.context["campaign_spec"], {})
        self.assertIsNotNone(s.context["_pending_elicitation"])


class CaptureGateTests(unittest.TestCase):
    def test_resume_fires_on_agentic_turn1_despite_high_turn_count(self):
        s = _session(_dur_pe(), "30 days", turn=5)            # _turn_count restored to 5 on resume
        _cap(s, turn=1)                                       # agentic-loop turn==1
        self.assertEqual(s.context["campaign_spec"].get("duration"), "30 days")

    def test_turn2_is_noop(self):
        s = _session(_dur_pe(), "30 days")
        self.assertEqual(_cap(s, turn=2), "")
        self.assertEqual(s.context["campaign_spec"], {})
        self.assertIsNotNone(s.context["_pending_elicitation"])

    def test_untagged_elicitation_noop(self):
        s = _session({"tool": "confirm_location", "expects": "single"}, "confirm")  # no field
        self.assertEqual(_cap(s), "")

    def test_account_pick_by_id(self):
        pe = {"tool": "present_options", "expects": "single", "field": "account",
              "answers": {"4461972633": "4461972633"}}
        s = _session(pe, "4461972633")
        s.context["account_names"] = {"4461972633": "Main Account"}  # populated by fetch tool
        _cap(s)
        self.assertEqual(s.context["campaign_spec"].get("account"), "4461972633")

    def test_account_pick_unknown_id_rejected(self):
        pe = {"tool": "present_options", "expects": "single", "field": "account",
              "answers": {"9999999999": "9999999999"}}
        s = _session(pe, "9999999999")
        s.context["account_names"] = {"4461972633": "Main Account"}
        self.assertEqual(_cap(s), "")
        self.assertEqual(s.context["campaign_spec"], {})


class NormalizationGuardTests(unittest.TestCase):
    ctx = {"product_data": RE}

    def test_canonical_match_accepts(self):
        self.assertTrue(_field_traceable("budget", "₹4,000/day", "4k", self.ctx))

    def test_off_topic_rejected(self):
        self.assertFalse(_field_traceable("budget", "₹4,000/day", "no competitors", self.ctx))

    def test_idempotent_resend_no_op(self):
        sc = {"product_data": RE, "campaign_spec": {"competitive_analysis_declined": "true"}, "_spec_set_at": {}}
        # caller (set_campaign_spec) filters unchanged values before _apply_field;
        # the guard itself still passes "true" on a "No" reply.
        self.assertTrue(_field_traceable("competitive_analysis_declined", "true", "No", sc))


class ParserTableTests(unittest.TestCase):
    def test_table(self):
        cases = [
            ("duration", "25 days", "$", "25 days"),
            ("duration", "a week", "$", "1 week"),
            ("duration", "2 months", "$", "2 months"),
            ("duration", "30 days no make it 60", "$", None),
            ("duration", "asap", "$", None),
            ("budget", "4k", "₹", "₹4,000/day"),
            ("budget", "₹4000/day", "₹", "₹4,000/day"),
            ("budget", "4000", "₹", None),
            ("budget", "$50", "₹", "$50/day"),
            ("platform", "facebook", "$", "Meta"),
            ("platform", "not google", "$", None),
            ("platform", "google or meta", "$", None),
            ("competitive_analysis_declined", "No", "$", None),  # not parseable; chip-only
        ]
        for field, text, cur, exp in cases:
            self.assertEqual(parse_typed_answer(field, text, cur), exp, f"{field} {text!r}")


def _untagged_present_options(missing: list[str]) -> list[str]:
    """Audit: a data-ask present_options prescription that forgot field=."""
    # Flags a present_options CALL (paren syntax) missing field= — catches a
    # doctored leak-style prescription. F16-reframed asks use prose ("use the
    # present_options tool (field \"x\")"), not call syntax, so field-presence
    # for those is asserted separately below.
    return [m for m in missing if "present_options(" in m and "field=" not in m]


def _cctx(spec):
    return CampaignContext(
        product={"business_type": "real estate"}, product_profile={}, competitor_names=[],
        competitor_analysis_attempted=False, spec=spec, account_names={}, set_at={},
        current_turn=1, last_user="", pending_location=None,
    )


class PrescriptionAuditTests(unittest.TestCase):
    def test_platform_ask_is_tagged(self):
        missing = _next_action(_cctx({}))
        self.assertEqual(_untagged_present_options(missing), [])
        self.assertTrue(any('field "platform"' in m for m in missing))

    def test_google_asks_are_tagged(self):
        # platform set → competitor + duration + budget asks render
        missing = _next_action(_cctx({"platform": "Google Ads"}))
        self.assertEqual(_untagged_present_options(missing), [])
        joined = "\n".join(missing)
        self.assertIn('field "competitive_analysis_declined"', joined)
        self.assertIn('field "duration"', joined)
        self.assertIn('field "budget"', joined)

    def test_audit_has_teeth(self):
        doctored = ["duration — call `present_options(question=\"How long?\", options=[...])`"]
        self.assertEqual(len(_untagged_present_options(doctored)), 1)


class PresentOptionsTagTests(unittest.TestCase):
    def test_tagged_returns_answer_map_on_data(self):
        res = asyncio.run(_present_options(
            {"question": "How long?",
             "options": [{"label": "30 days", "value": "30 days", "answer": "30 days"}, "Custom"],
             "field": "duration"},
            {"session_context": {}}))
        self.assertEqual(res.data["elicit_field"], "duration")
        self.assertEqual(res.data["elicit_answers"], {"30 days": "30 days"})  # Custom excluded

    def test_untagged_data_is_none(self):
        res = asyncio.run(_present_options(
            {"question": "Launch?", "options": ["Yes", "No"]},
            {"session_context": {}}))
        self.assertIsNone(res.data)


if __name__ == "__main__":
    unittest.main()
