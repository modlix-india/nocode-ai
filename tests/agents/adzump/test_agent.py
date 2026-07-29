"""AdzumpAgent orchestration seams: _capture_tagged_answer,
_resume_elicitation_section, _record_prose_decline, _next_action (workflow tree
incl. the Instagram-optional branch), get_pending_suggestions, _advance_chip,
_is_custom_reply, CampaignContext.from_session.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump.test_agent -v
"""
from __future__ import annotations

import asyncio
import types
import unittest
from unittest import mock

from app.agents.adzump.agent import (
    AdzumpAgent, CampaignContext, _is_custom_reply, _next_action,
)
from tests.agents.adzump._fixtures import (
    RE, SAAS, elicitation, make_cctx, make_session,
)


def _cap(s, turn=1):
    return AdzumpAgent._capture_tagged_answer(None, s, turn=turn)


def _suggest(s, text):
    return asyncio.run(AdzumpAgent.get_pending_suggestions(None, s, text))


def _dur_pe():
    return elicitation("duration", {"30 days": "30 days", "60 days": "60 days"})


def _budget_pe(**extra):
    return elicitation("budget", {"₹5,000/day": "₹5,000/day",
                                  "₹10,000/day": "₹10,000/day",
                                  "₹25,000/day": "₹25,000/day"}, **extra)


# ── Meta creative inspiration - consent-gated offer ─────────────────────────
class CompetitorCreativesOfferTests(unittest.TestCase):
    META = {"platform": "Meta", "duration": "30 days", "budget": "$50/day",
            "parent_account": "P", "account": "A", "fb_page": "F", "ig_page": "I"}

    def _offer_lines(self, **kw):
        m = _next_action(make_cctx(dict(self.META), product=SAAS, **kw))
        return [x for x in m if "competitor creatives" in x]

    def test_unoffered_prescribes_the_ask_once(self):
        cases = [
            ("with analysis", dict(competitor_names=["Rival"], attempted=True),
             "Want to see the ads your competitors are running right now?"),
            ("without analysis", {},
             "Want me to analyze your competitors and show the ads they're running?"),
        ]
        for name, kw, question in cases:
            with self.subTest(case=name):
                offer = self._offer_lines(**kw)
                self.assertEqual(len(offer), 1)
                self.assertIn("present_options", offer[0])
                self.assertIn("competitor_creatives_declined", offer[0])
                self.assertIn(question, offer[0])

    def test_offered_plus_yes_prescribes_fetch_not_reask(self):
        # regression: live 2026-07-27 - after a Yes chip click the verbatim ask
        # re-fired (nothing marks a Yes as resolving the offer) and the model
        # copied it instead of fetching.
        cases = [
            ("yes, competitors known", ["Rival"], True, "Yes",
             "call `fetch_competitor_creatives`"),
            ("yes, no analysis yet", [], False, "Yes",
             "run `analyze_competitors`, THEN `fetch_competitor_creatives`"),
            ("creative verbs count as yes", ["Rival"], True, "show me their ads",
             "call `fetch_competitor_creatives`"),
        ]
        for name, names, attempted, reply, chain in cases:
            with self.subTest(case=name):
                offer = self._offer_lines(competitor_names=names, attempted=attempted,
                                          creatives_offered=True, last_user=reply)
                self.assertEqual(len(offer), 1)
                self.assertIn(chain, offer[0])
                self.assertIn("said YES", offer[0])
                # The verbatim question must be gone - it's what the model copied.
                self.assertNotIn("Want me to analyze your competitors", offer[0])
                self.assertNotIn("Want to see the ads", offer[0])

    def test_offered_plus_unclear_reply_prescribes_react_not_fresh_ask(self):
        offer = self._offer_lines(creatives_offered=True,
                                  last_user="what will this cost me?")
        self.assertEqual(len(offer), 1)
        self.assertIn("ALREADY offered", offer[0])
        self.assertNotIn("Want me to analyze your competitors", offer[0])
        self.assertNotIn("Want to see the ads", offer[0])

    def test_from_session_reads_offered_marker(self):
        offered = make_session(spec=dict(self.META),
                               _competitor_creatives_offered=True)
        self.assertTrue(
            CampaignContext.from_session(offered).competitor_creatives_offered)
        self.assertFalse(
            CampaignContext.from_session(make_session(spec=dict(self.META)))
            .competitor_creatives_offered)

    def test_offer_is_suppressed_when_resolved(self):
        # Declined/fetched/moot resolution is computed by the shared predicate
        # (covered in test_campaign_data); _next_action only honours the flag.
        cases = [
            ("offer resolved", make_cctx(dict(self.META), product=SAAS,
                                         competitor_names=["R"], attempted=True,
                                         creatives_resolved=True)),
            ("google flow", make_cctx({**self.META, "platform": "Google Ads"},
                                      product=SAAS, competitor_names=["R"],
                                      attempted=True)),
        ]
        for name, cctx in cases:
            with self.subTest(case=name):
                m = _next_action(cctx)
                self.assertFalse(any("competitor creatives" in x for x in m))


# ── F3 · Instagram is optional ──────────────────────────────────────────────
class InstagramOptionalTests(unittest.TestCase):
    META_FULL = {"platform": "Meta", "duration": "30 days", "budget": "$50/day",
                 "parent_account": "P", "account": "A", "fb_page": "F"}

    def test_skip_cues_and_decline_traceability(self):
        # regression: F3 (Instagram optional)
        from app.agents.adzump.tools.campaign_data import (
            ALLOWED_FIELDS, _field_traceable, is_ig_skip,
        )
        self.assertIn("ig_page_declined", ALLOWED_FIELDS)
        for text, expected in [
            ("skip insta page", True), ("lets do it later", True),
            ("facebook only", True), ("continue with facebook only", True),
            ("no instagram", True), ("skip", True),
            ("link instagram", False), ("yes please", False),
            ("proceed", False), ("pick the first one", False),
        ]:
            with self.subTest(text=text):
                self.assertEqual(bool(is_ig_skip(text)), expected)
        ctx = {"product_data": dict(RE), "campaign_spec": {}, "_spec_set_at": {}}
        for user, expected in [
            ("Continue with Facebook only", True), ("skip insta", True),
            ("yes link instagram", False),
        ]:
            with self.subTest(user=user):
                self.assertEqual(
                    _field_traceable("ig_page_declined", "true", user, ctx), expected)

    def test_next_action_offers_ig_once(self):
        # regression: F3 (Instagram optional)
        m = _next_action(make_cctx(dict(self.META_FULL), product=SAAS))
        self.assertTrue(any("fetch_meta_ig_accounts" in x for x in m))

    def test_next_action_skip_cue_declines(self):
        # regression: F3 (Instagram optional)
        m = _next_action(make_cctx(dict(self.META_FULL), product=SAAS,
                                   last_user="skip insta page"))
        self.assertTrue(any("ig_page_declined" in x for x in m))
        self.assertFalse(any("fetch_meta_ig_accounts" in x for x in m))

    def test_next_action_offered_does_not_refetch(self):
        # regression: F3 (Instagram optional) / v5 (fetch≠render)
        m = _next_action(make_cctx(dict(self.META_FULL), product=SAAS,
                                   last_user="proceed", ig_offered=True))
        # The offered-branch may *name* the tool in a "do NOT call it again"
        # instruction - discriminate on the offer-branch's prescription syntax,
        # which is the thing that must be absent.
        self.assertFalse(any("Call `fetch_meta_ig_accounts(page_id=" in x for x in m))
        # v5 · fetch-time ≠ render-time: the reminder must not claim chips are
        # on screen (the model trusted that and skipped present_options live).
        self.assertFalse(any("ALREADY on screen" in x for x in m))
        self.assertTrue(any("already fetched" in x for x in m))
        self.assertTrue(any("present_options" in x for x in m))

    def test_declined_drops_ig_and_reaches_review(self):
        # regression: F3 (Instagram optional). The Meta creatives offer is
        # answered too - like IG, it's a once-asked step that precedes review
        # (from_session derives creatives_resolved from the declined flag).
        spec = {**self.META_FULL, "ig_page_declined": "true",
                "competitor_creatives_declined": "true"}
        m = _next_action(make_cctx(spec, product=SAAS, creatives_resolved=True))
        self.assertTrue(any("review" in x.lower() for x in m))
        self.assertFalse(any("instagram" in x.lower() and "fetch" in x.lower() for x in m))

    def test_review_gate_waits_for_ig_offer_or_decline(self):
        # regression: F3 (Instagram optional)
        from app.agents.adzump.tools.campaign_data import _review_hint_if_complete
        # fb_page set but IG neither picked nor declined → not complete yet
        self.assertEqual(
            _review_hint_if_complete(dict(self.META_FULL), {"product_data": SAAS}), "")
        answered = {**self.META_FULL, "ig_page_declined": "true",
                    "competitor_creatives_declined": "true"}
        hint = _review_hint_if_complete(answered, {"product_data": SAAS})
        self.assertNotEqual(hint, "")
        self.assertIn("not linked (Facebook only)", hint)

    def test_review_gate_waits_for_creatives_offer_resolution(self):
        # Meta review also waits on the competitor-creatives offer: declined,
        # fetched, or moot (analysis found zero rivals) all unblock it.
        from app.agents.adzump.tools.campaign_data import _review_hint_if_complete
        spec = {**self.META_FULL, "ig_page_declined": "true"}
        cases = [
            ("unresolved", {}, dict(spec), False),
            ("declined", {}, {**spec, "competitor_creatives_declined": "true"}, True),
            ("fetched", {"competitor_analysis": {"competitors": [
                {"name": "R", "creatives": [{"creativeId": "1"}]}]}}, dict(spec), True),
            ("moot - zero rivals", {"competitor_analysis": {"competitors": []}},
             dict(spec), True),
        ]
        for name, extra_ctx, case_spec, complete in cases:
            with self.subTest(case=name):
                hint = _review_hint_if_complete(
                    case_spec, {"product_data": SAAS, **extra_ctx})
                self.assertEqual(bool(hint), complete)


# ── PR2 / F17b · tagged-answer capture ──────────────────────────────────────
class TaggedCaptureTests(unittest.TestCase):
    """Chip answers and tight typed values store with provenance and consume
    the elicitation; ambiguity falls through to the model (F17b: an ambiguous
    "no…" must never auto-record the competitor decline)."""

    def test_table(self):
        decline = elicitation("competitive_analysis_declined")
        creatives_decline = elicitation("competitor_creatives_declined")
        cases = [  # (name, pe, user, stored, consumed)
            ("creatives decline chip", creatives_decline, "No",
             {"competitor_creatives_declined": "true"}, True),
            ("creatives typed clear decline", creatives_decline, "no thanks, skip it",
             {"competitor_creatives_declined": "true"}, True),
            ("creatives yes falls through to model", creatives_decline, "Yes", {}, False),
            ("duration chip", _dur_pe(), "30 days",
             {"duration": "30 days"}, True),
            ("budget preset chip", _budget_pe(), "₹10,000/day",
             {"budget": "₹10,000/day"}, True),
            ("decline chip", decline, "No",
             {"competitive_analysis_declined": "true"}, True),
            ("typed clear decline", decline, "no thanks, skip it",  # the live F17b message
             {"competitive_analysis_declined": "true"}, True),
            ("yes falls through to model", decline, "Yes", {}, False),
            ("defer with question", decline,
             "not now, first tell me about the audience", {}, False),
            ("informing, not declining", decline, "no competitors named yet", {}, False),
            ("typed duration", _dur_pe(), "25 days", {"duration": "25 days"}, True),
            ("typed budget with marker", elicitation("budget", {}), "4k",
             {"budget": "₹4,000/day"}, True),
            ("typed budget no marker", elicitation("budget", {}), "around 4000", {}, False),
            ("cross-field correction", _dur_pe(), "make it Meta", {}, False),
            ("untagged elicitation", {"tool": "confirm_location", "expects": "single"},
             "confirm", {}, False),
            ("account pick by known id",
             elicitation("account", {"4461972633": "4461972633"}), "4461972633",
             {"account": "4461972633"}, True),
            ("account pick unknown id rejected",
             elicitation("account", {"9999999999": "9999999999"}), "9999999999", {}, False),
        ]
        for name, pe, user, stored, consumed in cases:
            with self.subTest(name):
                s = make_session(last_user=user, pending_elicitation=dict(pe),
                                 account_names={"4461972633": "Main Account"})
                ack = _cap(s)
                self.assertEqual(s.context["campaign_spec"], stored)
                if consumed:
                    self.assertNotIn("_pending_elicitation", s.context)
                    for f in stored:
                        self.assertEqual(s.context["_spec_set_at"].get(f), 1)  # provenance
                else:
                    self.assertEqual(ack, "")
                    self.assertIsNotNone(s.context.get("_pending_elicitation"))

    def test_decline_capture_acknowledges(self):
        # regression: D14 (acknowledgement steer on deterministic capture)
        s = make_session(last_user="No",
                         pending_elicitation=elicitation("competitive_analysis_declined"))
        self.assertTrue(_cap(s))

    def test_turn_gate(self):
        # regression: PR2 (resume gated on agentic-loop turn, not _turn_count)
        s = make_session(last_user="30 days", pending_elicitation=_dur_pe(), turn=5)
        _cap(s, turn=1)                                       # resume restores _turn_count=5
        self.assertEqual(s.context["campaign_spec"].get("duration"), "30 days")
        s = make_session(last_user="30 days", pending_elicitation=_dur_pe())
        self.assertEqual(_cap(s, turn=2), "")                 # later agentic turns are noops
        self.assertEqual(s.context["campaign_spec"], {})
        self.assertIsNotNone(s.context["_pending_elicitation"])


# ── F4 · one-run capture marker (reliable post-capture ask) ─────────────────
class CaptureMarkerTests(unittest.TestCase):
    def test_marker_set_on_capture(self):
        # regression: F4 (reliable post-capture ask)
        s = make_session(last_user="30 days", pending_elicitation=_dur_pe())
        _cap(s)
        self.assertEqual(s.context.get("_captured_this_turn"), "duration")

    def test_suppresses_infer_when_captured(self):
        # regression: F4 (reliable post-capture ask)
        s = make_session(_captured_this_turn="duration")
        sentinel = mock.AsyncMock(return_value={"options": [], "mode": "single"})
        with mock.patch("app.agents.adzump.agent.infer_suggestions", new=sentinel):
            res = _suggest(s, "How long should it run?")
        self.assertIsNone(res)                                   # suppressed
        sentinel.assert_not_awaited()                            # infer never reached
        self.assertNotIn("_captured_this_turn", s.context)       # popped

    def test_no_marker_reaches_infer(self):
        # regression: F4 (over-suppress control - without the marker, infer runs)
        s = make_session()
        sentinel = mock.AsyncMock(return_value={"options": [1], "mode": "single"})
        with mock.patch("app.agents.adzump.agent.infer_suggestions", new=sentinel):
            res = _suggest(s, "How long?")
        sentinel.assert_awaited_once()
        self.assertEqual(res, {"options": [1], "mode": "single"})

    def test_marker_popped_even_on_early_return(self):
        # regression: F4 (marker must not leak to the next turn)
        s = make_session(
            _captured_this_turn="duration",
            _pending_suggestions={"options": [{"label": "x", "value": "x"}], "mode": "single"})
        res = _suggest(s, "")
        self.assertEqual(res["options"][0]["value"], "x")        # explicit chips returned
        self.assertNotIn("_captured_this_turn", s.context)       # still popped - no leak


# ── F10 · "Custom" chip → free-text ─────────────────────────────────────────
class CustomChipFreeTextTests(unittest.TestCase):
    def test_is_custom_reply(self):
        # regression: F10 ("Custom" → free-text)
        for text, expected in [
            ("Custom", True), ("custom", True), ("custom amount", True),
            ("Custom budget", True),
            ("₹5,000/day", False), ("30 days", False), ("Meta", False), ("", False),
        ]:
            with self.subTest(text=text):
                self.assertEqual(bool(_is_custom_reply(text)), expected)

    def test_custom_click_keeps_elicitation_open_and_steers(self):
        # regression: F10 - "Custom" isn't a value: keep the elicitation OPEN,
        # mark awaiting_custom, return a free-text steer (live bug #11 was
        # re-rendering the same chips instead).
        for pe in (_budget_pe(), _dur_pe()):
            with self.subTest(field=pe["field"]):
                s = make_session(last_user="Custom", pending_elicitation=pe)
                ack = _cap(s)
                self.assertIn("custom value", ack.lower())
                self.assertTrue(s.context["_pending_elicitation"].get("awaiting_custom"))
                self.assertEqual(s.context["campaign_spec"], {})   # not stored

    def test_typed_value_after_custom_is_captured(self):
        # regression: F10 ("Custom" → free-text)
        s = make_session(last_user="₹7000",
                         pending_elicitation=_budget_pe(awaiting_custom=True))
        _cap(s)
        self.assertEqual(s.context["campaign_spec"].get("budget"), "₹7,000/day")
        self.assertNotIn("_pending_elicitation", s.context)        # consumed on capture

    def test_offtopic_is_not_mistaken_for_custom(self):
        # regression: F10 ("Custom" → free-text)
        s = make_session(last_user="what does daily budget mean?",
                         pending_elicitation=_budget_pe())
        self.assertEqual(_cap(s), "")
        self.assertFalse(s.context["_pending_elicitation"].get("awaiting_custom"))

    def test_resume_keeps_open_when_awaiting_custom(self):
        # regression: F10 ("Custom" → free-text)
        s = make_session(last_user="ok",
                         pending_elicitation=_budget_pe(awaiting_custom=True))
        out = AdzumpAgent._resume_elicitation_section(None, s, turn=1)
        self.assertEqual(out, "")
        self.assertIsNotNone(s.context.get("_pending_elicitation"))  # NOT popped
        self.assertTrue(s.context["_pending_elicitation"].get("awaiting_custom"))

    def test_next_action_free_text_when_awaiting_chips_otherwise(self):
        # regression: F10 ("Custom" → free-text)
        spec = {"platform": "Meta", "duration": "30 days",
                "parent_account": "P", "account": "A"}
        budget = [x for x in _next_action(make_cctx(spec, product=SAAS, awaiting="budget"))
                  if x.startswith("budget")]
        self.assertTrue(budget)
        self.assertIn("TYPE", budget[0])
        # The free-text prescription may *name* present_options in a "do NOT
        # call" instruction - the chip-CALL signature is what must be absent.
        self.assertNotIn("present_options(question", budget[0])
        budget = [x for x in _next_action(make_cctx(spec, product=SAAS))
                  if x.startswith("budget")]
        self.assertTrue(budget)
        self.assertIn("present_options", budget[0])                # normal chip ask

    def test_from_session_resolves_awaiting_custom(self):
        # regression: F10 - from_session must NOT raise (the walrus-in-conditional
        # UnboundLocalError) and must resolve awaiting_custom_field; the other
        # F10 tests build CampaignContext directly, this exercises the live path.
        s = make_session(last_user="₹7000",
                         pending_elicitation=_budget_pe(awaiting_custom=True))
        self.assertEqual(CampaignContext.from_session(s).awaiting_custom_field, "budget")
        s = make_session(last_user="₹7000", pending_elicitation=_budget_pe())
        self.assertIsNone(CampaignContext.from_session(s).awaiting_custom_field)
        self.assertIsNone(CampaignContext.from_session(
            make_session(last_user="hi")).awaiting_custom_field)


# ── F18 · prose-offer typed decline recorded in code ────────────────────────
class ProseDeclineRecorderTests(unittest.TestCase):
    @staticmethod
    def _record(s, turn=1):
        cctx = CampaignContext.from_session(s)
        return AdzumpAgent._record_prose_decline(
            None, s, cctx, s.messages[-1]["content"], turn)

    def test_table(self):
        pe = elicitation("competitive_analysis_declined")
        cases = [  # (name, user, extra_spec, pe, turn, recorded)
            ("clear typed decline", "no thanks, skip it", {}, None, 1, True),
            ("ambiguous defer", "not now, first tell me about the audience",
             {}, None, 1, False),
            ("informing, not declining", "no competitors named yet", {}, None, 1, False),
            ("pending elicitation defers to tagged capture", "no", {}, pe, 1, False),
            ("already attempted is a noop", "no thanks",
             {"competitive_analysis_declined": "true"}, None, 1, False),
            ("turn 2 is a noop", "no thanks, skip it", {}, None, 2, False),
        ]
        for name, user, extra_spec, pe_, turn, recorded in cases:
            with self.subTest(name):
                s = make_session(last_user=user,
                                 spec={"platform": "Google Ads", **extra_spec},
                                 pending_elicitation=dict(pe_) if pe_ else None,
                                 turn=turn)
                self.assertEqual(self._record(s, turn=turn), recorded)
                if not extra_spec:
                    self.assertEqual(
                        "competitive_analysis_declined" in s.context["campaign_spec"],
                        recorded)


# ── F20 · review/publish prescription must not leak tool-call syntax ────────
def _full_google_cctx():
    # "full" = every _next_action gate satisfied, so review & publish is the
    # only prescription left. The geo gate checks the nested platform handle
    # on target_areas (platform.is_mapped_for).
    spec = {
        "platform": "Google Ads", "location": "Bengaluru", "duration": "30 days",
        "budget": "₹10,000/day", "competitive_analysis_declined": "true",
        "parent_account": "1234567890", "account": "4461972633",
    }
    product = {
        "business_type": "real estate", "product_name": "Sumadhura Solea",
        "target_areas": [{"name": "Bengaluru",
                          "google": {"resourceName": "geoTargetConstants/1026181"}}],
    }
    return make_cctx(spec, product=product, attempted=True,
                     account_names={"1234567890": "MCC", "4461972633": "Acct"})


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


# ── F23/F27 · value-only advance chip for prose asks ────────────────────────
# F27 live bug (run M3): at the launch step the model wrote the summary +
# "Ready to launch the campaign?" as PROSE; the whole-blob match saw "ready to"
# + the "Location:" summary bullet → emitted a misleading "Confirm location"
# chip. Fix: evaluate the TRAILING line + launch→location→generic precedence.
_LAUNCH_SUMMARY = (
    "Here's your campaign summary:\n\n"
    "- Product: Concorde Neo\n"
    "- Location: Thanisandra Main Rd, Bengaluru, Karnataka, India\n"
    "- Platform: Meta\n"
    "- Duration: 60 days\n"
    "- Daily Budget: ₹7,500/day\n\n"
    "Ready to launch the campaign?"
)


class AdvanceChipTests(unittest.TestCase):
    def test_helper_table(self):
        # regression: F23 (advance chip) + F27 (launch-step precedence)
        cases = [  # (text, expected chip value or None, expected label or None)
            ("Let's confirm the location for the campaign.",
             "yes, confirm the location", None),
            ("Shall I go ahead and set this up for you?", "yes, go ahead", None),
            (_LAUNCH_SUMMARY, "yes, launch", "Yes, launch"),   # THE F27 lock
            ("Location set. Ready to launch?", "yes, launch", None),  # launch beats location
            ("I've noted the location.\n\nShall I proceed?",   # last-line anchoring
             "yes, go ahead", None),
            ("Great, almost done.\n\nLet's confirm the location for the campaign.",
             "yes, confirm the location", None),               # F23 preserved with lead-in
            ("What is your daily budget?", None, None),        # data ask, not an advance
            ("", None, None),
        ]
        for text, value, label in cases:
            with self.subTest(text=text[:48]):
                chip = AdzumpAgent._advance_chip(text)
                if value is None:
                    self.assertIsNone(chip)
                    continue
                self.assertEqual(chip["mode"], "single")
                opt = chip["options"][0]
                self.assertEqual(opt["value"], value)
                if label:
                    self.assertEqual(opt["label"], label)
                self.assertNotIn("field", opt)   # value-only → can't reintroduce F4
                self.assertNotIn("answer", opt)

    def test_advance_chip_fires_even_after_capture(self):
        # regression: F23 + F4 no-regression in one: user just answered a chip
        # (_captured_this_turn set), model asks the next step as prose → one
        # VALUE-ONLY chip, and the capture marker is still popped.
        s = make_session(last_user="30 days", spec={"platform": "Google Ads"},
                         _captured_this_turn="platform")
        res = _suggest(s, "Got it. Let's confirm the location for the campaign.")
        self.assertIsNotNone(res)
        self.assertEqual(len(res["options"]), 1)
        self.assertEqual(res["options"][0]["value"], "yes, confirm the location")
        self.assertNotIn("_captured_this_turn", s.context)   # popped (no leak)

    def test_no_chip_on_data_prose_after_capture(self):
        # regression: F23 / F4 protection - a data-collection prose re-ask
        # (NOT an advance ask) after a capture → None; model re-asks via a
        # tagged tool next turn.
        s = make_session(last_user="30 days", spec={"platform": "Google Ads"},
                         _captured_this_turn="platform")
        self.assertIsNone(_suggest(s, "What's your daily budget?"))
        self.assertNotIn("_captured_this_turn", s.context)   # still popped

    def test_no_advance_chip_when_widget_pending(self):
        # regression: F23 - a live elicitation owns the turn
        s = make_session(last_user="30 days", spec={"platform": "Google Ads"},
                         pending_elicitation={"tool": "present_options", "field": "duration"})
        self.assertIsNone(_suggest(s, "Let's confirm the location for the campaign."))


# ── D9 · every data-ask present_options carries field= ─────────────────────
def _untagged_present_options(missing: list[str]) -> list[str]:
    """Audit: a data-ask present_options prescription that forgot field=."""
    # Flags a present_options CALL (paren syntax) missing field= - catches a
    # doctored leak-style prescription. F16-reframed asks use prose ("use the
    # present_options tool (field \"x\")"), not call syntax, so field-presence
    # for those is asserted separately below.
    return [m for m in missing if "present_options(" in m and "field=" not in m]


class PrescriptionAuditTests(unittest.TestCase):
    def test_data_asks_are_tagged(self):
        # regression: D9 (every data-ask present_options carries field=)
        missing = _next_action(make_cctx({}))
        self.assertEqual(_untagged_present_options(missing), [])
        self.assertTrue(any('field "platform"' in m for m in missing))
        # platform set → competitor + duration + budget asks render
        missing = _next_action(make_cctx({"platform": "Google Ads"}))
        self.assertEqual(_untagged_present_options(missing), [])
        joined = "\n".join(missing)
        self.assertIn('field "competitive_analysis_declined"', joined)
        self.assertIn('field "duration"', joined)
        self.assertIn('field "budget"', joined)

    def test_audit_has_teeth(self):
        # regression: D9 (every data-ask present_options carries field=)
        doctored = ["duration - call `present_options(question=\"How long?\", options=[...])`"]
        self.assertEqual(len(_untagged_present_options(doctored)), 1)


# ── one-shot resume gate ────────────────────────────────────────────────────
class ResumeGateTests(unittest.TestCase):
    def test_table(self):
        cases = [  # (name, pe, turn, rendered, pe_survives)
            ("turn1 renders and pops",
             {"expects": "single", "tool": "confirm_location"}, 1, True, False),
            ("turn2 empty and does not pop",
             {"expects": "single", "tool": "confirm_location"}, 2, False, True),
            ("no pending", None, 1, False, False),
        ]
        for name, pe, turn, rendered, survives in cases:
            with self.subTest(name):
                s = types.SimpleNamespace(
                    context={"_pending_elicitation": dict(pe)} if pe else {})
                out = AdzumpAgent._resume_elicitation_section(None, s, turn=turn)
                self.assertEqual(bool(out), rendered)
                self.assertEqual("_pending_elicitation" in s.context, survives)


if __name__ == "__main__":
    unittest.main()
