"""F17 · competitor-decline value-bleed + breaker blind spot (below the model).

Live bug: user declines competitor analysis ("no thanks, skip it") on the Google
path. The prescription said `set …declined to "true"`; the model copied "true"
onto EVERY missing field (value-bleed), all rejected, and — because the bleed
call also re-sent already-stored fields — the result was a `partial` (success,
some kept) that NEITHER breaker counted → 18 consecutive set_campaign_spec calls.

Three fixes, locked here:
  (b) F17c — a set_campaign_spec call that stores NOTHING new flags no_progress,
      even when it kept/rejected fields, so the core stuck-step breaker counts it.
  (a) F17a — even if the model bleeds, only the traceable declined field lands.
  (★) F17b — a clear decline (chip OR tight typed reply) is recorded in code;
      ambiguous "no…" replies fall through to the model (no false-record).

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump.test_v6_fixes -v
"""
from __future__ import annotations

import asyncio
import types
import unittest

from app.agents.adzump.agent import AdzumpAgent, _next_action, CampaignContext
from app.agents.adzump.tools.campaign_data import (
    _set_campaign_spec, is_clear_decline_reply,
)

RE = {"product_name": "Sumadhura Solea", "summary": "Luxury 3 & 4 BHK apartments.",
      "business_type": "real estate"}
ADDR = "3J8G+23, Rachenahalli, Thanisandra, Bengaluru, Karnataka 560045, India"


def _spec_ctx(spec, last_user):
    sc = {"campaign_spec": dict(spec), "_spec_set_at": {}, "product_data": dict(RE)}
    session = types.SimpleNamespace(
        messages=[{"role": "user", "content": last_user}], _turn_count=7,
    )
    return {"session_context": sc, "_session": session}, sc


# ── (b) F17c — the breaker blind spot: partial that stores nothing ──
class NoProgressFloorTests(unittest.TestCase):
    def test_kept_plus_rejected_storing_nothing_flags_no_progress(self):
        # exact F17c shape: location kept (paraphrase of stored), duration="true"
        # rejected (invented). Nothing NEW stored → must flag no_progress so the
        # stuck-step breaker counts it (this is what looped 18×).
        ctx, sc = _spec_ctx({"location": ADDR}, "")
        r = asyncio.run(_set_campaign_spec(
            {"location": "Bengaluru", "duration": "true"}, ctx))
        self.assertTrue(r.success)                                  # partial = success
        self.assertTrue(isinstance(r.data, dict) and r.data.get("no_progress"))
        self.assertNotIn("duration", sc["campaign_spec"])           # "true" not stored

    def test_partial_that_stores_something_is_not_no_progress(self):
        # boundary: a real store + a rejected invent is genuine progress → no flag,
        # so a legit correction bundled with a stray field never trips the breaker.
        ctx, sc = _spec_ctx({}, "make it 30 days")
        r = asyncio.run(_set_campaign_spec(
            {"duration": "30 days", "budget": "true"}, ctx))
        self.assertTrue(r.success)
        self.assertEqual(sc["campaign_spec"]["duration"], "30 days")
        self.assertFalse(isinstance(r.data, dict) and r.data.get("no_progress"))


# ── (a) F17a — bleed containment: only the traceable declined field lands ──
class BleedContainmentTests(unittest.TestCase):
    def test_decline_bleed_stores_only_the_declined_field(self):
        ctx, sc = _spec_ctx({"platform": "Google Ads"}, "no thanks, skip it")
        r = asyncio.run(_set_campaign_spec({
            "competitive_analysis_declined": "true", "duration": "true",
            "budget": "true", "account": "true",
        }, ctx))
        self.assertTrue(r.success)
        self.assertEqual(sc["campaign_spec"].get("competitive_analysis_declined"), "true")
        for f in ("duration", "budget", "account"):
            self.assertNotIn(f, sc["campaign_spec"])               # bleed contained


# ── (★) F17b — record the decline deterministically (chip + tight typed) ──
def _session(user, *, field="competitive_analysis_declined", answers=None, spec=None, turn=1):
    s = types.SimpleNamespace()
    s.context = {
        "_pending_elicitation": {"tool": "present_options", "expects": "single",
                                 "field": field, "answers": answers or {"No": "true"}},
        "campaign_spec": dict(spec or {}),
        "_spec_set_at": {},
        "product_data": dict(RE),
    }
    s.messages = [{"role": "user", "content": user}]
    s._turn_count = turn
    return s


def _cap(s, turn=1):
    return AdzumpAgent._capture_tagged_answer(None, s, turn=turn)


class TaggedDeclineCaptureTests(unittest.TestCase):
    def test_no_chip_records_decline(self):
        s = _session("No")
        _cap(s)
        self.assertEqual(s.context["campaign_spec"].get("competitive_analysis_declined"), "true")
        self.assertNotIn("_pending_elicitation", s.context)         # consumed

    def test_typed_clear_decline_records(self):
        s = _session("no thanks, skip it")                          # the live bug message
        _cap(s)
        self.assertEqual(s.context["campaign_spec"].get("competitive_analysis_declined"), "true")

    def test_yes_falls_through_to_model(self):
        s = _session("Yes")
        self.assertEqual(_cap(s), "")
        self.assertEqual(s.context["campaign_spec"], {})
        self.assertIsNotNone(s.context["_pending_elicitation"])     # LLM runs analyze_competitors

    # the regression guard that matters most — ambiguous "no…" must NOT auto-record
    def test_defer_with_question_does_not_record(self):
        s = _session("not now, first tell me about the audience")
        self.assertEqual(_cap(s), "")
        self.assertEqual(s.context["campaign_spec"], {})
        self.assertIsNotNone(s.context["_pending_elicitation"])

    def test_informing_no_competitors_does_not_record(self):
        s = _session("no competitors named yet")
        self.assertEqual(_cap(s), "")
        self.assertEqual(s.context["campaign_spec"], {})


class ClearDeclineReplyTableTests(unittest.TestCase):
    def test_table(self):
        clear = ["no", "n", "no thanks", "no thanks, skip it", "skip it",
                 "No, skip competitor analysis", "not now", "maybe later", "no need"]
        ambiguous = ["no competitors named yet", "not now, first tell me about the audience",
                     "no, make it Meta", "what about competitors?", "no — which ones?",
                     "skip — but tell me how it works"]
        for t in clear:
            self.assertTrue(is_clear_decline_reply(t), f"should be a clear decline: {t!r}")
        for t in ambiguous:
            self.assertFalse(is_clear_decline_reply(t), f"should NOT auto-record: {t!r}")


def _full_google_cctx():
    spec = {
        "platform": "Google Ads", "location": "Bengaluru", "duration": "30 days",
        "budget": "₹10,000/day", "competitive_analysis_declined": "true",
        "parent_account": "1234567890", "account": "4461972633",
    }
    return CampaignContext(
        product={"business_type": "real estate", "product_name": "Sumadhura Solea"},
        product_profile={}, competitor_names=[], competitor_analysis_attempted=True,
        spec=spec, account_names={"1234567890": "MCC", "4461972633": "Acct"},
        set_at={}, current_turn=1, last_user="", pending_location=None,
    )


# ── F20 — the review/publish prescription must not leak tool-call syntax ──
class ReviewPublishPrescriptionTests(unittest.TestCase):
    def test_review_publish_has_no_raw_tool_call_syntax(self):
        missing = _next_action(_full_google_cctx())
        review = next((m for m in missing if "review & publish" in m), None)
        self.assertIsNotNone(review, f"review&publish should appear; got: {missing}")
        # F20: the live leak was the model echoing this prescription's copyable
        # present_options(...)/launch_campaign() syntax into the launch bubble.
        self.assertNotIn("present_options(", review)
        self.assertNotIn("launch_campaign(", review)
        # but it must still instruct CALLING those tools (intent prose form)
        self.assertIn("present_options tool", review)
        self.assertIn("launch_campaign tool", review)


if __name__ == "__main__":
    unittest.main()
