"""Adversarial-input PROBE — drives the capture core with the same human inputs
the 10-run live script (plans/adversarial-test-plan.md) uses, at the cheapest
layer (no browser). Each test mirrors one live scenario:

  G2  duration correction "no wait, make it 60" + budget "actually ₹25,000/day"
  M2  platform correction  "actually switch to Meta"
  G5  volunteered multi-field first message (platform+duration+budget at once)
  M4  volunteered location+duration
  G5  edge inputs: empty send, emoji, whitespace (must not crash / false-record)

These are PROBES, not regression locks: a failure here is a candidate finding to
route through the log→visual-plan→panel→fix loop, not necessarily a confirmed bug
(the live model may phrase the set_campaign_spec call differently). Comments mark
which assertions are the live hypothesis under test.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump.test_adversarial_probe -v
"""
from __future__ import annotations

import asyncio
import types
import unittest

from app.agents.adzump.tools.campaign_data import (
    _set_campaign_spec, _field_traceable, is_clear_decline_reply,
)
from app.agents.adzump.answer_parse import field_candidates
from app.agents.adzump.agent import AdzumpAgent

RE = {"product_name": "Sumadhura Solea", "summary": "Luxury 3 & 4 BHK apartments.",
      "business_type": "real estate"}


def _ctx(spec, last_user, turn=7):
    sc = {"campaign_spec": dict(spec), "_spec_set_at": {}, "product_data": dict(RE)}
    session = types.SimpleNamespace(
        messages=[{"role": "user", "content": last_user}], _turn_count=turn)
    return {"session_context": sc, "_session": session}, sc


def _set(spec, last_user, params):
    ctx, sc = _ctx(spec, last_user)
    r = asyncio.run(_set_campaign_spec(params, ctx))
    return r, sc


# ── G2 · duration correction with a cue word ──────────────────────────────
class DurationCorrectionTests(unittest.TestCase):
    def test_correction_canonical_form(self):
        # HYPOTHESIS: user already at 30d, then "no wait, make it 60". The model
        # sends the canonical "60 days". parse_typed_answer bails on the cue
        # ("no"/"wait") so traceability falls to raw substring — "60 days" is NOT
        # literally in "no wait, make it 60" → REJECTED. If this fails, G2 breaks.
        r, sc = _set({"duration": "30 days"}, "no wait, make it 60",
                     {"duration": "60 days"})
        self.assertEqual(sc["campaign_spec"]["duration"], "60 days",
                         f"correction lost; summary={r.summary!r} error={r.error!r}")

    def test_correction_bare_number_form(self):
        # Model echoes the bare "60". The correction must LAND (not be dropped) —
        # that's F24's bar. Storing raw "60" vs canonical "60 days" is a cosmetic
        # write-path nicety descoped per the panel (Boris); assert 60-ish, not 30.
        r, sc = _set({"duration": "30 days"}, "no wait, make it 60",
                     {"duration": "60"})
        self.assertIn("60", sc["campaign_spec"].get("duration", ""),
                      f"correction dropped; stored={sc['campaign_spec'].get('duration')!r}")
        self.assertNotEqual(sc["campaign_spec"].get("duration"), "30 days")

    def test_simple_correction_no_cue(self):
        # Control: a correction phrased WITHOUT a cue word ("change duration to 60
        # days") — should always work.
        r, sc = _set({"duration": "30 days"}, "change duration to 60 days",
                     {"duration": "60 days"})
        self.assertEqual(sc["campaign_spec"]["duration"], "60 days")


# ── G2 · budget correction ────────────────────────────────────────────────
class BudgetCorrectionTests(unittest.TestCase):
    def test_budget_correction_with_symbol(self):
        # "actually ₹25,000/day" — has the cue "actually", but the canonical
        # "₹25,000/day" IS a substring → should pass via raw match.
        r, sc = _set({"budget": "₹10,000/day"}, "actually ₹25,000/day",
                     {"budget": "₹25,000/day"})
        self.assertEqual(sc["campaign_spec"]["budget"], "₹25,000/day",
                         f"summary={r.summary!r} error={r.error!r}")

    def test_budget_correction_normalized_differs(self):
        # The riskier shape: user types "actually make it 25k/day", model sends the
        # normalized "₹25,000/day". Cue → parse bails; "₹25,000/day" not a substring
        # of "...25k/day" → candidate for rejection.
        r, sc = _set({"budget": "₹10,000/day"}, "actually make it 25k/day",
                     {"budget": "₹25,000/day"})
        self.assertEqual(sc["campaign_spec"]["budget"], "₹25,000/day",
                         f"summary={r.summary!r} error={r.error!r}")


# ── M2 · platform correction ──────────────────────────────────────────────
class PlatformCorrectionTests(unittest.TestCase):
    def test_switch_to_meta(self):
        # "actually switch to Meta" — platform branch uses Platform.from_value on
        # both sides, independent of parse_typed_answer's cue guard → should pass.
        r, sc = _set({"platform": "Google Ads"}, "actually switch to Meta",
                     {"platform": "Meta"})
        self.assertEqual(sc["campaign_spec"]["platform"], "Meta",
                         f"summary={r.summary!r} error={r.error!r}")


# ── G5 / M4 · volunteered multi-field first message ───────────────────────
class VolunteeredMultiFieldTests(unittest.TestCase):
    def test_google_duration_budget_volunteered(self):
        # G5 first message: "make a google ads campaign for X, 60 day duration,
        # ₹15000/day budget". Model sends all three. HYPOTHESIS: duration+budget
        # fail because the message has >1 distinct number → parse_typed_answer
        # returns None for BOTH, and the canonical forms aren't literal substrings.
        msg = ("make a google ads campaign for adanithane.com, "
               "60 day duration, ₹15000/day budget")
        r, sc = _set({}, msg, {"platform": "Google Ads",
                               "duration": "60 days", "budget": "₹15,000/day"})
        got = sc["campaign_spec"]
        self.assertEqual(got.get("platform"), "Google Ads")
        self.assertEqual(got.get("duration"), "60 days",
                         f"volunteered duration lost; stored={got}")
        self.assertEqual(got.get("budget"), "₹15,000/day",
                         f"volunteered budget lost; stored={got}")

    def test_volunteered_echo_forms(self):
        # If the model echoes the user's exact tokens ("60 day", "₹15000/day"),
        # substring match should save them even with 2 numbers present.
        msg = ("make a google ads campaign, 60 day duration, ₹15000/day budget")
        r, sc = _set({}, msg, {"platform": "Google Ads",
                               "duration": "60 day", "budget": "₹15000/day"})
        got = sc["campaign_spec"]
        self.assertIn("duration", got, f"stored={got}")
        self.assertIn("budget", got, f"stored={got}")


# ── G5 · edge inputs must not crash or false-record ───────────────────────
class EdgeInputTests(unittest.TestCase):
    def test_empty_send(self):
        r, sc = _set({"platform": "Google Ads"}, "", {"duration": "30 days"})
        # value not traceable to an empty message → rejected, no crash, not stored
        self.assertNotIn("duration", sc["campaign_spec"])

    def test_emoji_only(self):
        self.assertFalse(is_clear_decline_reply("👍"))
        r, sc = _set({"platform": "Google Ads"}, "👍", {"duration": "30 days"})
        self.assertNotIn("duration", sc["campaign_spec"])

    def test_whitespace_only(self):
        self.assertFalse(is_clear_decline_reply("   "))
        r, sc = _set({}, "   ", {"platform": "Google Ads"})
        self.assertNotIn("platform", sc["campaign_spec"])

    def test_emoji_does_not_false_decline(self):
        # an emoji reply to the competitor offer must NOT auto-record a decline
        self.assertFalse(is_clear_decline_reply("🤔"))
        self.assertFalse(is_clear_decline_reply("👍 sounds good"))


# ── G4 / M3 · Custom duration/budget escape → typed value (two-turn flow) ──
# Confirmation lock (not a bug hunt): picking "Custom" must keep the elicitation
# open (awaiting_custom) and capture the typed value next turn — the F10 path.
class CustomPathTests(unittest.TestCase):
    def _ses(self, field, answers, user, *, awaiting=False):
        pe = {"tool": "present_options", "expects": "single",
              "field": field, "answers": dict(answers)}
        if awaiting:
            pe["awaiting_custom"] = True
        s = types.SimpleNamespace()
        s.context = {"_pending_elicitation": pe, "campaign_spec": {"platform": "Google Ads"},
                     "_spec_set_at": {}, "product_data": dict(RE)}
        s.messages = [{"role": "user", "content": user}]
        s._turn_count = 1
        return s

    def _cap(self, s):
        return AdzumpAgent._capture_tagged_answer(None, s, turn=1)

    def test_custom_duration_two_turns(self):
        ans = {"30 days": "30 days", "60 days": "60 days", "90 days": "90 days"}
        a = self._ses("duration", ans, "Custom")                  # turn A: pick Custom
        steer = self._cap(a)
        self.assertIn("custom", steer.lower())
        self.assertTrue(a.context["_pending_elicitation"].get("awaiting_custom"))
        self.assertNotIn("duration", a.context["campaign_spec"])  # not captured yet
        b = self._ses("duration", ans, "45 days", awaiting=True)  # turn B: type it
        self._cap(b)
        self.assertEqual(b.context["campaign_spec"].get("duration"), "45 days")
        self.assertNotIn("_pending_elicitation", b.context)       # consumed

    def test_custom_budget_two_turns(self):
        ans = {"₹10,000/day": "₹10,000/day", "₹20,000/day": "₹20,000/day"}
        a = self._ses("budget", ans, "custom amount")
        self._cap(a)
        self.assertTrue(a.context["_pending_elicitation"].get("awaiting_custom"))
        b = self._ses("budget", ans, "₹7,500/day", awaiting=True)
        self._cap(b)
        self.assertEqual(b.context["campaign_spec"].get("budget"), "₹7,500/day")

    def test_custom_then_bare_number_duration(self):
        # after Custom, a bare "45" reads as days canonically (F1 bare-int path)
        b = self._ses("duration", {"30 days": "30 days"}, "45", awaiting=True)
        self._cap(b)
        self.assertEqual(b.context["campaign_spec"].get("duration"), "45 days")


# ── direct traceability matrix (was root-cause; now fix-confirmation) ─────
class TraceabilityMatrixTests(unittest.TestCase):
    """These two PINNED the bug pre-fix (both returned False). Post-F24 they
    return True — a cue-bearing correction and a multi-number message are now
    traceable via field_candidates. Kept as before→after fix locks."""

    def test_cue_word_correction_is_now_traceable(self):
        sc = {"product_data": dict(RE)}
        self.assertTrue(
            _field_traceable("duration", "60 days", "no wait, make it 60", sc),
            "F24: cue-word corrections must be traceable")

    def test_multinumber_duration_is_now_traceable(self):
        sc = {"product_data": dict(RE)}
        self.assertTrue(
            _field_traceable("duration", "60 days",
                             "60 day duration, ₹15000/day budget", sc),
            "F24: volunteered multi-number duration must be traceable")

    def test_clean_single_value_is_traceable(self):
        sc = {"product_data": dict(RE)}
        self.assertTrue(_field_traceable("duration", "60 days", "make it 60 days", sc))


# ── F24 FIX · the firewall: the negatives a careless fix gets wrong ───────
# These lock the anti-invention property the loosening must NOT break (Lance:
# canonical-equality, never digit-substring; Kiran: budget marker LOCALITY).
class F24FirewallTests(unittest.TestCase):
    SC = {"product_data": dict(RE)}

    # positives — the loosening must accept these (were silently dropped)
    def test_cue_correction_accepts(self):
        self.assertTrue(_field_traceable("duration", "60 days", "no wait, make it 60", self.SC))

    def test_normalized_budget_correction_accepts(self):
        self.assertTrue(_field_traceable("budget", "₹25,000/day", "actually make it 25k/day", self.SC))

    def test_multinumber_duration_accepts(self):
        self.assertTrue(_field_traceable(
            "duration", "60 days", "60 day duration, ₹15000/day budget", self.SC))

    def test_multinumber_budget_accepts(self):
        self.assertTrue(_field_traceable(
            "budget", "₹15,000/day", "60 day duration, ₹15000/day budget", self.SC))

    # negatives — invention / cross-assignment / F1 must STILL reject
    def test_invented_number_rejected(self):  # Lance: THE anti-invention test
        self.assertFalse(_field_traceable("duration", "45 days", "no wait, make it 60", self.SC))

    def test_budget_free_number_rejected(self):  # Kiran: local-vs-global marker
        self.assertFalse(_field_traceable(
            "budget", "₹15/day", "make the daily budget ₹500/day, i have 15 properties", self.SC))

    def test_cross_assign_duration_number_not_a_budget(self):
        self.assertFalse(_field_traceable(
            "budget", "₹60/day", "60 day duration, ₹15000/day budget", self.SC))

    def test_cross_assign_budget_number_not_a_duration(self):
        self.assertFalse(_field_traceable(
            "duration", "15000 days", "60 day duration, ₹15000/day budget", self.SC))

    def test_f1_duration_leak_still_closed(self):
        self.assertFalse(_field_traceable("duration", "5 days", "I have 15 properties", self.SC))

    def test_f1_budget_leak_still_closed(self):
        self.assertFalse(_field_traceable("budget", "₹5,000/day", "call me at 5000", self.SC))

    def test_wrong_unit_not_cross_matched(self):
        # user said weeks; model storing days for the same number must NOT pass
        self.assertFalse(_field_traceable("duration", "3 days", "make it 3 weeks", self.SC))


# ── extractor-level locks (field_candidates directly) ─────────────────────
class FieldCandidatesTests(unittest.TestCase):
    def test_budget_local_marker_only(self):
        c = field_candidates("budget", "60 day duration, ₹15000/day budget", "₹")
        self.assertIn("₹15,000/day", c)
        self.assertNotIn("₹60/day", c)            # 60 has no LOCAL money marker

    def test_duration_unit_and_free(self):
        c = field_candidates("duration", "60 day duration, ₹15000/day budget", "₹")
        self.assertIn("60 days", c)
        self.assertNotIn("15000 days", c)         # 15000 is money, not days

    def test_duration_free_bare_in_correction(self):
        self.assertEqual(field_candidates("duration", "no wait, make it 60", "₹"), {"60 days"})

    def test_duration_property_count_not_money_but_number_equality_guards(self):
        # "15 properties" yields a 15-days candidate, but anti-invention rests on
        # the model's value matching — an invented 5 still finds no 5 here.
        c = field_candidates("duration", "make it 60 days, i have 15 properties", "₹")
        self.assertEqual(c, {"60 days", "15 days"})

    def test_budget_bare_number_yields_nothing(self):
        self.assertEqual(field_candidates("budget", "call me at 5000", "₹"), set())

    def test_whole_string_bare_int(self):
        self.assertEqual(field_candidates("duration", "30", "₹"), {"30 days"})


if __name__ == "__main__":
    unittest.main()
