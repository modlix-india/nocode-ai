"""Unit: app/agents/adzump/agent.py — AdzumpAgent orchestration helpers.

Covers the agent-level seams with no other home:
  _capture_tagged_answer / _resume_elicitation_section (tagged-answer capture +
  one-shot resume gate), _record_prose_decline, _next_action (workflow tree,
  incl. the Instagram-optional branch), get_pending_suggestions, _advance_chip,
  _is_custom_reply, and CampaignContext.from_session.

Migrated verbatim from the old fix-campaign files (test_v3/v4/v6_fixes,
test_tagged_capture, test_tail_reminder); behavior-identical, classes renamed to
read by behavior. Local `_session`/`_cctx`/`_cap`/… helpers are kept per-source
where bodies diverge (different RE shapes, different CampaignContext params) so
the rewire can't shift behavior.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump.test_agent -v
"""
from __future__ import annotations

import asyncio
import types
import unittest
from unittest import mock

from app.agents.adzump.agent import (
    AdzumpAgent, CampaignContext, _next_action, _is_custom_reply,
)

RE = {"business_type": "real estate", "product_name": "Skyline Villas"}
SAAS = {"business_type": "saas", "product_name": "Acme"}


def _ctx(spec=None, **extra):
    c = {"product_data": RE, "campaign_spec": dict(spec or {}), "_spec_set_at": {}}
    c.update(extra)
    return c


def _cctx(spec, *, product=None, last_user="", ig_offered=False, account_names=None):
    return CampaignContext(
        product=product if product is not None else dict(SAAS), product_profile={},
        competitor_names=[], competitor_analysis_attempted=False, spec=spec,
        account_names=account_names or {}, set_at={}, current_turn=1,
        last_user=last_user, pending_location=None, ig_offered=ig_offered,
    )


# ── _next_action · Instagram optional (was F3IgOptionalTests) ──────────────
class InstagramOptionalTests(unittest.TestCase):
    META_FULL = {"platform": "Meta", "duration": "30 days", "budget": "$50/day",
                 "parent_account": "P", "account": "A", "fb_page": "F"}

    def test_ig_page_declined_is_allowed_field(self):
        # regression: F3 (Instagram optional)
        from app.agents.adzump.tools.campaign_data import ALLOWED_FIELDS
        self.assertIn("ig_page_declined", ALLOWED_FIELDS)

    def test_is_ig_skip_recognizes_optouts(self):
        # regression: F3 (Instagram optional)
        from app.agents.adzump.tools.campaign_data import is_ig_skip
        for s in ("skip insta page", "lets do it later", "facebook only",
                  "continue with facebook only", "no instagram", "skip"):
            self.assertTrue(is_ig_skip(s), s)
        for s in ("link instagram", "yes please", "proceed", "pick the first one"):
            self.assertFalse(is_ig_skip(s), s)

    def test_decline_flag_traceable(self):
        # regression: F3 (Instagram optional)
        from app.agents.adzump.tools.campaign_data import _field_traceable
        self.assertTrue(_field_traceable("ig_page_declined", "true", "Continue with Facebook only", _ctx()))
        self.assertTrue(_field_traceable("ig_page_declined", "true", "skip insta", _ctx()))
        self.assertFalse(_field_traceable("ig_page_declined", "true", "yes link instagram", _ctx()))

    def test_next_action_offers_ig_once(self):
        # regression: F3 (Instagram optional)
        m = _next_action(_cctx(dict(self.META_FULL)))
        self.assertTrue(any("fetch_meta_ig_accounts" in x for x in m))

    def test_next_action_skip_cue_declines(self):
        # regression: F3 (Instagram optional)
        m = _next_action(_cctx(dict(self.META_FULL), last_user="skip insta page"))
        self.assertTrue(any("ig_page_declined" in x for x in m))
        self.assertFalse(any("fetch_meta_ig_accounts" in x for x in m))

    def test_next_action_offered_does_not_refetch(self):
        # regression: F3 (Instagram optional) / v5 (fetch≠render)
        m = _next_action(_cctx(dict(self.META_FULL), last_user="proceed", ig_offered=True))
        # The offered-branch may *name* the tool in a "do NOT call it again"
        # instruction — discriminate on the offer-branch's prescription syntax,
        # which is the thing that must be absent.
        self.assertFalse(any("Call `fetch_meta_ig_accounts(page_id=" in x for x in m))
        # v5 · fetch-time ≠ render-time: the reminder must not claim chips are
        # on screen (the model trusted that and skipped present_options live).
        self.assertFalse(any("ALREADY on screen" in x for x in m))
        self.assertTrue(any("already fetched" in x for x in m))
        self.assertTrue(any("present_options" in x for x in m))

    def test_declined_drops_ig_and_reaches_review(self):
        # regression: F3 (Instagram optional)
        spec = {**self.META_FULL, "ig_page_declined": "true"}
        m = _next_action(_cctx(spec))
        self.assertTrue(any("review" in x.lower() for x in m))
        self.assertFalse(any("instagram" in x.lower() and "fetch" in x.lower() for x in m))

    def test_review_gate_waits_until_ig_offered(self):
        # regression: F3 (Instagram optional)
        from app.agents.adzump.tools.campaign_data import _review_hint_if_complete
        # fb_page set but IG neither picked nor declined → not complete yet
        self.assertEqual(_review_hint_if_complete(dict(self.META_FULL), {"product_data": SAAS}), "")

    def test_review_gate_complete_on_facebook_only(self):
        # regression: F3 (Instagram optional)
        from app.agents.adzump.tools.campaign_data import _review_hint_if_complete
        spec = {**self.META_FULL, "ig_page_declined": "true"}
        hint = _review_hint_if_complete(spec, {"product_data": SAAS})
        self.assertNotEqual(hint, "")
        self.assertIn("not linked (Facebook only)", hint)


# ── _capture_tagged_answer · one-run capture marker (was F4CaptureMarkerTests) ──
def _dur_pe():
    return {"tool": "present_options", "expects": "single",
            "field": "duration", "answers": {"30 days": "30 days"}}


def _session(pe, user, *, spec=None):
    s = types.SimpleNamespace()
    s.context = {"_pending_elicitation": dict(pe) if pe else None,
                 "campaign_spec": dict(spec or {}), "_spec_set_at": {}, "product_data": RE}
    s.messages = [{"role": "user", "content": user}]
    s._turn_count = 1
    return s


class CaptureMarkerTests(unittest.TestCase):
    def test_marker_set_on_capture(self):
        # regression: F4 (reliable post-capture ask)
        s = _session(_dur_pe(), "30 days")
        AdzumpAgent._capture_tagged_answer(None, s, turn=1)
        self.assertEqual(s.context.get("_captured_this_turn"), "duration")

    def test_suppresses_infer_when_captured(self):
        # regression: F4 (reliable post-capture ask)
        s = types.SimpleNamespace(); s.context = {"_captured_this_turn": "duration"}
        sentinel = mock.AsyncMock(return_value={"options": [], "mode": "single"})
        with mock.patch("app.agents.adzump.agent.infer_suggestions", new=sentinel):
            res = asyncio.run(AdzumpAgent.get_pending_suggestions(None, s, "How long should it run?"))
        self.assertIsNone(res)                                   # suppressed
        sentinel.assert_not_awaited()                            # infer never reached
        self.assertNotIn("_captured_this_turn", s.context)       # popped

    def test_no_marker_reaches_infer(self):
        # regression: F4 (reliable post-capture ask)
        # Over-suppress control: without the marker, infer IS reached.
        s = types.SimpleNamespace(); s.context = {}
        sentinel = mock.AsyncMock(return_value={"options": [1], "mode": "single"})
        with mock.patch("app.agents.adzump.agent.infer_suggestions", new=sentinel):
            res = asyncio.run(AdzumpAgent.get_pending_suggestions(None, s, "How long?"))
        sentinel.assert_awaited_once()
        self.assertEqual(res, {"options": [1], "mode": "single"})

    def test_marker_popped_even_on_early_return(self):
        # regression: F4 (reliable post-capture ask)
        # Leak invariant: explicit chips short-circuit, but the marker is popped
        # up-front so it can't survive to the next turn.
        s = types.SimpleNamespace()
        s.context = {"_captured_this_turn": "duration",
                     "_pending_suggestions": {"options": [{"label": "x", "value": "x"}], "mode": "single"}}
        res = asyncio.run(AdzumpAgent.get_pending_suggestions(None, s, ""))
        self.assertEqual(res["options"][0]["value"], "x")        # explicit chips returned
        self.assertNotIn("_captured_this_turn", s.context)       # still popped — no leak


# ── _is_custom_reply / "Custom" → free-text (was F10CustomTests) ───────────
def _custom_session(pe, user, *, spec=None, product=RE):
    s = types.SimpleNamespace()
    s.context = {"_pending_elicitation": dict(pe) if pe else None,
                 "campaign_spec": dict(spec or {}), "_spec_set_at": {}, "product_data": dict(product)}
    s.messages = [{"role": "user", "content": user}]
    s._turn_count = 1
    return s


def _budget_pe(**extra):
    pe = {"tool": "present_options", "expects": "single", "field": "budget",
          "answers": {"₹5,000/day": "₹5,000/day", "₹10,000/day": "₹10,000/day",
                      "₹25,000/day": "₹25,000/day"}}
    pe.update(extra)
    return pe


def _custom_cctx(spec, *, awaiting=None, product=None, last_user=""):
    return CampaignContext(
        product=product or dict(SAAS), product_profile={}, competitor_names=[],
        competitor_analysis_attempted=False, spec=spec, account_names={}, set_at={},
        current_turn=1, last_user=last_user, pending_location=None, ig_offered=False,
        awaiting_custom_field=awaiting,
    )


class CustomChipFreeTextTests(unittest.TestCase):
    def test_is_custom_reply(self):
        # regression: F10 ("Custom" → free-text)
        for s in ("Custom", "custom", "custom amount", "Custom budget"):
            self.assertTrue(_is_custom_reply(s), s)
        for s in ("₹5,000/day", "30 days", "Meta", ""):
            self.assertFalse(_is_custom_reply(s), s)

    def test_custom_click_keeps_elicitation_open_and_steers(self):
        # regression: F10 ("Custom" → free-text)
        s = _custom_session(_budget_pe(), "Custom")
        ack = AdzumpAgent._capture_tagged_answer(None, s, turn=1)
        self.assertIn("custom value", ack.lower())                 # free-text steer
        self.assertTrue(s.context["_pending_elicitation"].get("awaiting_custom"))  # kept open + marked
        self.assertNotIn("budget", s.context["campaign_spec"])     # NOT stored ("Custom" isn't a value)

    def test_typed_value_after_custom_is_captured(self):
        # regression: F10 ("Custom" → free-text)
        s = _custom_session(_budget_pe(awaiting_custom=True), "₹7000")
        AdzumpAgent._capture_tagged_answer(None, s, turn=1)
        self.assertEqual(s.context["campaign_spec"].get("budget"), "₹7,000/day")
        self.assertNotIn("_pending_elicitation", s.context)        # consumed on capture

    def test_preset_click_still_captures(self):
        # regression: F10 ("Custom" → free-text)
        s = _custom_session(_budget_pe(), "₹10,000/day")
        AdzumpAgent._capture_tagged_answer(None, s, turn=1)
        self.assertEqual(s.context["campaign_spec"].get("budget"), "₹10,000/day")
        self.assertNotIn("_pending_elicitation", s.context)

    def test_offtopic_is_not_mistaken_for_custom(self):
        # regression: F10 ("Custom" → free-text)
        s = _custom_session(_budget_pe(), "what does daily budget mean?")
        ack = AdzumpAgent._capture_tagged_answer(None, s, turn=1)
        self.assertEqual(ack, "")
        self.assertFalse(s.context["_pending_elicitation"].get("awaiting_custom"))  # not marked

    def test_resume_keeps_open_when_awaiting_custom(self):
        # regression: F10 ("Custom" → free-text)
        s = _custom_session(_budget_pe(awaiting_custom=True), "ok")
        out = AdzumpAgent._resume_elicitation_section(None, s, turn=1)
        self.assertEqual(out, "")
        self.assertIsNotNone(s.context.get("_pending_elicitation"))  # NOT popped
        self.assertTrue(s.context["_pending_elicitation"].get("awaiting_custom"))

    def test_next_action_prescribes_free_text_when_awaiting(self):
        # regression: F10 ("Custom" → free-text)
        m = _next_action(_custom_cctx({"platform": "Meta", "duration": "30 days",
                                       "parent_account": "P", "account": "A"}, awaiting="budget"))
        budget = [x for x in m if x.startswith("budget")]
        self.assertTrue(budget, m)
        self.assertIn("TYPE", budget[0])
        # The free-text prescription may *name* present_options in a "do NOT call"
        # instruction — discriminate on the chip-CALL signature, which must be absent.
        self.assertNotIn("present_options(question", budget[0])     # no chip ask

    def test_next_action_shows_chips_when_not_awaiting(self):
        # regression: F10 ("Custom" → free-text)
        m = _next_action(_custom_cctx({"platform": "Meta", "duration": "30 days",
                                       "parent_account": "P", "account": "A"}))
        budget = [x for x in m if x.startswith("budget")]
        self.assertTrue(budget, m)
        self.assertIn("present_options", budget[0])                # normal chip ask

    def test_from_session_resolves_awaiting_custom(self):
        # regression: F10 ("Custom" → free-text)
        # Regression: from_session must NOT raise (the walrus-in-conditional
        # UnboundLocalError) and must resolve awaiting_custom_field. The other
        # F10 tests build CampaignContext directly, bypassing from_session —
        # this exercises the live path.
        self.assertEqual(
            CampaignContext.from_session(_custom_session(_budget_pe(awaiting_custom=True), "₹7000")).awaiting_custom_field,
            "budget")
        self.assertIsNone(  # elicitation present but not awaiting
            CampaignContext.from_session(_custom_session(_budget_pe(), "₹7000")).awaiting_custom_field)
        self.assertIsNone(  # no elicitation at all
            CampaignContext.from_session(_custom_session(None, "hi")).awaiting_custom_field)


# ── competitor-decline capture + prose recorder + advance chip (was test_v6_fixes) ──
RE_V6 = {"product_name": "Sumadhura Solea", "summary": "Luxury 3 & 4 BHK apartments.",
         "business_type": "real estate"}


def _v6_session(user, *, field="competitive_analysis_declined", answers=None, spec=None, turn=1):
    s = types.SimpleNamespace()
    s.context = {
        "_pending_elicitation": {"tool": "present_options", "expects": "single",
                                 "field": field, "answers": answers or {"No": "true"}},
        "campaign_spec": dict(spec or {}),
        "_spec_set_at": {},
        "product_data": dict(RE_V6),
    }
    s.messages = [{"role": "user", "content": user}]
    s._turn_count = turn
    return s


def _v6_cap(s, turn=1):
    return AdzumpAgent._capture_tagged_answer(None, s, turn=turn)


class TaggedDeclineCaptureTests(unittest.TestCase):
    def test_no_chip_records_decline(self):
        # regression: F17b (competitor-decline deterministic record)
        s = _v6_session("No")
        _v6_cap(s)
        self.assertEqual(s.context["campaign_spec"].get("competitive_analysis_declined"), "true")
        self.assertNotIn("_pending_elicitation", s.context)         # consumed

    def test_typed_clear_decline_records(self):
        # regression: F17b (competitor-decline deterministic record)
        s = _v6_session("no thanks, skip it")                       # the live bug message
        _v6_cap(s)
        self.assertEqual(s.context["campaign_spec"].get("competitive_analysis_declined"), "true")

    def test_yes_falls_through_to_model(self):
        # regression: F17b (competitor-decline deterministic record)
        s = _v6_session("Yes")
        self.assertEqual(_v6_cap(s), "")
        self.assertEqual(s.context["campaign_spec"], {})
        self.assertIsNotNone(s.context["_pending_elicitation"])     # LLM runs analyze_competitors

    # the regression guard that matters most — ambiguous "no…" must NOT auto-record
    def test_defer_with_question_does_not_record(self):
        # regression: F17b (competitor-decline deterministic record)
        s = _v6_session("not now, first tell me about the audience")
        self.assertEqual(_v6_cap(s), "")
        self.assertEqual(s.context["campaign_spec"], {})
        self.assertIsNotNone(s.context["_pending_elicitation"])

    def test_informing_no_competitors_does_not_record(self):
        # regression: F17b (competitor-decline deterministic record)
        s = _v6_session("no competitors named yet")
        self.assertEqual(_v6_cap(s), "")
        self.assertEqual(s.context["campaign_spec"], {})


# ── F18 — record a prose-offer typed decline in code (no elicitation needed) ──
def _prose_session(user, *, spec=None, pe=None, turn=1):
    s = types.SimpleNamespace()
    s.context = {
        "_pending_elicitation": dict(pe) if pe else None,
        "campaign_spec": dict(spec or {"platform": "Google Ads"}),
        "_spec_set_at": {},
        "product_data": dict(RE_V6),
    }
    s.messages = [{"role": "user", "content": user}]
    s._turn_count = turn
    return s


def _record(s, turn=1):
    cctx = CampaignContext.from_session(s)
    return AdzumpAgent._record_prose_decline(
        None, s, cctx, s.messages[-1]["content"], turn)


class ProseDeclineRecorderTests(unittest.TestCase):
    def test_clear_typed_decline_recorded(self):
        # regression: F18 (prose-offer typed decline)
        s = _prose_session("no thanks, skip it")               # Google, no elicitation
        self.assertTrue(_record(s))
        self.assertEqual(s.context["campaign_spec"].get("competitive_analysis_declined"), "true")

    def test_ambiguous_defer_not_recorded(self):
        # regression: F18 (prose-offer typed decline)
        s = _prose_session("not now, first tell me about the audience")
        self.assertFalse(_record(s))
        self.assertNotIn("competitive_analysis_declined", s.context["campaign_spec"])

    def test_informing_no_competitors_not_recorded(self):
        # regression: F18 (prose-offer typed decline)
        s = _prose_session("no competitors named yet")
        self.assertFalse(_record(s))
        self.assertNotIn("competitive_analysis_declined", s.context["campaign_spec"])

    def test_pending_competitor_elicitation_defers_to_tagged_capture(self):
        # regression: F18 (prose-offer typed decline)
        s = _prose_session("no", pe={"tool": "present_options", "expects": "single",
                                     "field": "competitive_analysis_declined", "answers": {"No": "true"}})
        self.assertFalse(_record(s))                           # tagged-capture owns it

    def test_not_recorded_when_already_attempted(self):
        # regression: F18 (prose-offer typed decline)
        s = _prose_session("no thanks", spec={"platform": "Google Ads",
                                              "competitive_analysis_declined": "true"})
        self.assertFalse(_record(s))                           # already set → no-op

    def test_turn2_is_noop(self):
        # regression: F18 (prose-offer typed decline)
        s = _prose_session("no thanks, skip it")
        self.assertFalse(_record(s, turn=2))


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
        # regression: F20 (review/publish prescription leak)
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


# ── F23 — value-only "advance" chip for prose asks (no F4 re-ask regression) ──
class AdvanceChipTests(unittest.TestCase):
    def _ses(self, extra=None):
        s = types.SimpleNamespace()
        s.context = {"campaign_spec": {"platform": "Google Ads"}, "product_data": dict(RE_V6)}
        if extra:
            s.context.update(extra)
        s.messages = [{"role": "user", "content": "30 days"}]
        s._turn_count = 1
        return s

    def _call(self, s, text):
        agent = AdzumpAgent.__new__(AdzumpAgent)
        return asyncio.run(agent.get_pending_suggestions(s, text))

    def test_advance_chip_fires_even_after_capture(self):
        # regression: F23 (advance chip for prose asks) / F4 no-regression
        # THE dead-end case + F4 no-regression in one: user just answered a chip
        # (_captured_this_turn set), model asks the next step as prose → one
        # VALUE-ONLY chip, and the capture marker is still popped.
        s = self._ses({"_captured_this_turn": "platform"})
        res = self._call(s, "Got it. Let's confirm the location for the campaign.")
        self.assertIsNotNone(res)
        self.assertEqual(len(res["options"]), 1)
        opt = res["options"][0]
        self.assertNotIn("field", opt)                       # value-only → can't reintroduce F4
        self.assertNotIn("answer", opt)
        self.assertEqual(opt["value"], "yes, confirm the location")
        self.assertNotIn("_captured_this_turn", s.context)   # popped (no leak to next turn)

    def test_generic_advance_chip(self):
        # regression: F23 (advance chip for prose asks)
        s = self._ses()
        res = self._call(s, "Shall I go ahead and set this up for you?")
        self.assertEqual(res["options"][0]["value"], "yes, go ahead")

    def test_no_chip_on_data_prose_after_capture(self):
        # regression: F23 (advance chip for prose asks) / F4 protection
        # a data-collection prose re-ask (NOT an advance ask) after a capture → None
        # (F4 protection preserved; the model re-asks via a tagged tool next turn).
        s = self._ses({"_captured_this_turn": "platform"})
        res = self._call(s, "What's your daily budget?")
        self.assertIsNone(res)
        self.assertNotIn("_captured_this_turn", s.context)   # still popped

    def test_no_advance_chip_when_widget_pending(self):
        # regression: F23 (advance chip for prose asks)
        # a live elicitation owns the turn → no competing advance chip
        s = self._ses({"_pending_elicitation": {"tool": "present_options", "field": "duration"}})
        self.assertIsNone(self._call(s, "Let's confirm the location for the campaign."))

    def test_helper_value_only_and_gated(self):
        # regression: F23 (advance chip for prose asks)
        chip = AdzumpAgent._advance_chip("Let's confirm the location")
        self.assertEqual(chip["mode"], "single")
        self.assertTrue(all("field" not in o and "answer" not in o for o in chip["options"]))
        self.assertIsNone(AdzumpAgent._advance_chip("What is your daily budget?"))  # not an advance ask
        self.assertIsNone(AdzumpAgent._advance_chip(""))


# ── F27 · launch-step summary must NOT yield a "Confirm location" chip ─────
# Live bug (run M3): at the launch step the model wrote the summary + "Ready to
# launch the campaign?" as PROSE; the whole-blob match saw "ready to" + the
# "Location:" summary bullet → emitted a misleading "Confirm location" chip with
# NO "make changes" option. Fix: evaluate the TRAILING line + launch→location→
# generic precedence.
_LAUNCH_SUMMARY = (
    "Here's your campaign summary:\n\n"
    "- Product: Concorde Neo\n"
    "- Location: Thanisandra Main Rd, Bengaluru, Karnataka, India\n"
    "- Platform: Meta\n"
    "- Duration: 60 days\n"
    "- Daily Budget: ₹7,500/day\n\n"
    "Ready to launch the campaign?"
)


class AdvanceChipLaunchTests(unittest.TestCase):
    def test_launch_summary_not_confirm_location(self):           # THE F27 lock
        # regression: F27 (launch-step chip mis-fire)
        chip = AdzumpAgent._advance_chip(_LAUNCH_SUMMARY)
        self.assertIsNotNone(chip)
        opt = chip["options"][0]
        self.assertNotEqual(opt["label"], "Confirm location")
        self.assertNotEqual(opt["value"], "yes, confirm the location")

    def test_launch_summary_yields_yes_launch(self):
        # regression: F27 (launch-step chip mis-fire)
        opt = AdzumpAgent._advance_chip(_LAUNCH_SUMMARY)["options"][0]
        self.assertEqual(opt["value"], "yes, launch")
        self.assertEqual(opt["label"], "Yes, launch")
        self.assertNotIn("field", opt)                           # F4: still value-only
        self.assertNotIn("answer", opt)

    def test_launch_beats_location_keyword(self):
        # regression: F27 (launch-step chip mis-fire)
        # a single line containing BOTH must resolve to launch
        opt = AdzumpAgent._advance_chip("Location set. Ready to launch?")["options"][0]
        self.assertEqual(opt["value"], "yes, launch")

    def test_location_in_lead_line_not_trailing_generic_ask(self):
        # regression: F27 (launch-step chip mis-fire)
        # "location" only in an earlier line; trailing line is a generic advance →
        # "Go ahead", NOT "Confirm location" (proves last-line anchoring)
        opt = AdzumpAgent._advance_chip("I've noted the location.\n\nShall I proceed?")["options"][0]
        self.assertEqual(opt["value"], "yes, go ahead")

    def test_genuine_location_ask_with_lead_in_still_confirms(self):
        # regression: F27 (launch-step chip mis-fire) / F23 preserved
        # F23 preserved: a lead-in line + trailing location-confirm prose
        opt = AdzumpAgent._advance_chip(
            "Great, almost done.\n\nLet's confirm the location for the campaign.")["options"][0]
        self.assertEqual(opt["value"], "yes, confirm the location")


# ── tagged-answer capture — chip / typed / gate (was test_tagged_capture) ──
RE_TC = {"business_type": "real estate"}


def _tc_session(pe, user, *, spec=None, turn=1, product=RE_TC):
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


def _tc_cap(s, turn=1):
    return AdzumpAgent._capture_tagged_answer(None, s, turn=turn)


def _decline_pe(answers=None):
    return {"tool": "present_options", "expects": "single",
            "field": "competitive_analysis_declined", "answers": answers or {"No": "true"}}


def _tc_dur_pe():
    return {"tool": "present_options", "expects": "single",
            "field": "duration", "answers": {"30 days": "30 days", "60 days": "60 days"}}


class CaptureChipTests(unittest.TestCase):
    def test_competitor_no_stores_true_and_pops(self):
        # regression: PR2 (tagged-answer capture)
        s = _tc_session(_decline_pe(), "No")
        ack = _tc_cap(s)
        self.assertEqual(s.context["campaign_spec"].get("competitive_analysis_declined"), "true")
        self.assertNotIn("_pending_elicitation", s.context)   # consumed
        self.assertTrue(ack)                                  # acknowledgement steer (D14)

    def test_competitor_yes_falls_through(self):
        # regression: PR2 (tagged-answer capture)
        s = _tc_session(_decline_pe(), "Yes")                 # "Yes" not in answers map
        self.assertEqual(_tc_cap(s), "")
        self.assertEqual(s.context["campaign_spec"], {})
        self.assertIsNotNone(s.context["_pending_elicitation"])  # intact → LLM runs analyze_competitors

    def test_duration_chip_stores_and_provenance(self):
        # regression: PR2 (tagged-answer capture)
        s = _tc_session(_tc_dur_pe(), "30 days")
        _tc_cap(s)
        self.assertEqual(s.context["campaign_spec"].get("duration"), "30 days")
        self.assertEqual(s.context["_spec_set_at"].get("duration"), 1)

    def test_custom_triggers_freetext_steer(self):
        # regression: PR2 (tagged-answer capture) / v4 F10
        # v4 · F10 changed this: "Custom" (no answer) no longer falls through
        # silently — it keeps the elicitation OPEN, marks awaiting_custom, and
        # returns a free-text steer so the typed value is captured next turn
        # (instead of re-rendering the same chips, live bug #11). Still not stored.
        s = _tc_session(_tc_dur_pe(), "Custom")
        ack = _tc_cap(s)
        self.assertIn("custom value", ack.lower())            # F10 free-text steer
        self.assertEqual(s.context["campaign_spec"], {})      # "Custom" is not a value
        self.assertIsNotNone(s.context["_pending_elicitation"])           # kept open
        self.assertTrue(s.context["_pending_elicitation"].get("awaiting_custom"))


class CaptureTypedTests(unittest.TestCase):
    def test_typed_duration(self):
        # regression: PR2 / D12 (typed free-text capture)
        s = _tc_session(_tc_dur_pe(), "25 days")              # no exact match → parser
        _tc_cap(s)
        self.assertEqual(s.context["campaign_spec"].get("duration"), "25 days")

    def test_typed_budget_4k_real_estate(self):
        # regression: PR2 / D12 (typed free-text capture)
        s = _tc_session({"tool": "present_options", "expects": "single", "field": "budget", "answers": {}}, "4k")
        _tc_cap(s)
        self.assertEqual(s.context["campaign_spec"].get("budget"), "₹4,000/day")

    def test_typed_budget_no_marker_falls_through(self):
        # regression: PR2 / D12 (typed free-text capture)
        s = _tc_session({"tool": "present_options", "expects": "single", "field": "budget", "answers": {}}, "around 4000")
        self.assertEqual(_tc_cap(s), "")
        self.assertEqual(s.context["campaign_spec"], {})

    def test_cross_field_correction_falls_through(self):
        # regression: PR2 / D12 (typed free-text capture)
        s = _tc_session(_tc_dur_pe(), "make it Meta")         # not a duration → no store
        self.assertEqual(_tc_cap(s), "")
        self.assertEqual(s.context["campaign_spec"], {})
        self.assertIsNotNone(s.context["_pending_elicitation"])


class CaptureGateTests(unittest.TestCase):
    def test_resume_fires_on_agentic_turn1_despite_high_turn_count(self):
        # regression: PR2 (resume gated on agentic-loop turn)
        s = _tc_session(_tc_dur_pe(), "30 days", turn=5)      # _turn_count restored to 5 on resume
        _tc_cap(s, turn=1)                                    # agentic-loop turn==1
        self.assertEqual(s.context["campaign_spec"].get("duration"), "30 days")

    def test_turn2_is_noop(self):
        # regression: PR2 (resume gated on agentic-loop turn)
        s = _tc_session(_tc_dur_pe(), "30 days")
        self.assertEqual(_tc_cap(s, turn=2), "")
        self.assertEqual(s.context["campaign_spec"], {})
        self.assertIsNotNone(s.context["_pending_elicitation"])

    def test_untagged_elicitation_noop(self):
        # regression: PR2 (tagged-answer capture)
        s = _tc_session({"tool": "confirm_location", "expects": "single"}, "confirm")  # no field
        self.assertEqual(_tc_cap(s), "")

    def test_account_pick_by_id(self):
        # regression: PR2 (tagged-answer capture)
        pe = {"tool": "present_options", "expects": "single", "field": "account",
              "answers": {"4461972633": "4461972633"}}
        s = _tc_session(pe, "4461972633")
        s.context["account_names"] = {"4461972633": "Main Account"}  # populated by fetch tool
        _tc_cap(s)
        self.assertEqual(s.context["campaign_spec"].get("account"), "4461972633")

    def test_account_pick_unknown_id_rejected(self):
        # regression: PR2 (tagged-answer capture)
        pe = {"tool": "present_options", "expects": "single", "field": "account",
              "answers": {"9999999999": "9999999999"}}
        s = _tc_session(pe, "9999999999")
        s.context["account_names"] = {"4461972633": "Main Account"}
        self.assertEqual(_tc_cap(s), "")
        self.assertEqual(s.context["campaign_spec"], {})


def _untagged_present_options(missing: list[str]) -> list[str]:
    """Audit: a data-ask present_options prescription that forgot field=."""
    # Flags a present_options CALL (paren syntax) missing field= — catches a
    # doctored leak-style prescription. F16-reframed asks use prose ("use the
    # present_options tool (field \"x\")"), not call syntax, so field-presence
    # for those is asserted separately below.
    return [m for m in missing if "present_options(" in m and "field=" not in m]


def _audit_cctx(spec):
    return CampaignContext(
        product={"business_type": "real estate"}, product_profile={}, competitor_names=[],
        competitor_analysis_attempted=False, spec=spec, account_names={}, set_at={},
        current_turn=1, last_user="", pending_location=None,
    )


class PrescriptionAuditTests(unittest.TestCase):
    def test_platform_ask_is_tagged(self):
        # regression: D9 (every data-ask present_options carries field=)
        missing = _next_action(_audit_cctx({}))
        self.assertEqual(_untagged_present_options(missing), [])
        self.assertTrue(any('field "platform"' in m for m in missing))

    def test_google_asks_are_tagged(self):
        # regression: D9 (every data-ask present_options carries field=)
        # platform set → competitor + duration + budget asks render
        missing = _next_action(_audit_cctx({"platform": "Google Ads"}))
        self.assertEqual(_untagged_present_options(missing), [])
        joined = "\n".join(missing)
        self.assertIn('field "competitive_analysis_declined"', joined)
        self.assertIn('field "duration"', joined)
        self.assertIn('field "budget"', joined)

    def test_audit_has_teeth(self):
        # regression: D9 (every data-ask present_options carries field=)
        doctored = ["duration — call `present_options(question=\"How long?\", options=[...])`"]
        self.assertEqual(len(_untagged_present_options(doctored)), 1)


# ── _resume_elicitation_section · one-shot resume gate (was test_tail_reminder) ──
class ResumeGateTests(unittest.TestCase):
    @staticmethod
    def _session(pe):
        return types.SimpleNamespace(context=({"_pending_elicitation": pe} if pe else {}))

    def test_turn1_single_renders_and_pops(self):
        s = self._session({"expects": "single", "tool": "confirm_location"})
        out = AdzumpAgent._resume_elicitation_section(None, s, turn=1)
        self.assertTrue(out)                                  # rendered
        self.assertNotIn("_pending_elicitation", s.context)   # one-shot popped

    def test_turn2_empty_and_does_not_pop(self):
        s = self._session({"expects": "single", "tool": "confirm_location"})
        out = AdzumpAgent._resume_elicitation_section(None, s, turn=2)
        self.assertEqual(out, "")                             # not rendered on later turns
        self.assertIn("_pending_elicitation", s.context)      # flag survives (NOT popped)

    def test_turn1_no_pending(self):
        s = self._session(None)
        self.assertEqual(AdzumpAgent._resume_elicitation_section(None, s, turn=1), "")


if __name__ == "__main__":
    unittest.main()
