"""v3 reliability fixes — correctness guards (real logic, minimal mocks).

Covers the properties the v3 fixes introduce, and the regressions the panel
re-review flagged:

  F1  guard hardening — the loose digit-substring leak is gone; a typed bare
      number reads as days (duration-only) canonically, not via a leak.
  F2  dependency-clear cascade — a *changed* platform / parent / fb_page clears
      its stale dependents, never a same-call sibling, and never on re-send;
      plus the launch-boundary platform-gate refuses a cross-platform id.
  F3  Instagram optional — ig_page_declined traceability, the IG-pending
      next-action branch (offer once / honour skip / don't re-fetch), and the
      review gate (fb_page + (ig_page OR declined)).
  F4  reliable post-capture ask — the one-run capture marker is set, suppresses
      the untagged infer fallback, and is always popped (no leak to next turn).

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump.test_v3_fixes -v
"""
from __future__ import annotations

import asyncio
import types
import unittest
from unittest import mock

from app.agents.adzump.agent import AdzumpAgent, _next_action, CampaignContext
from app.agents.adzump.answer_parse import parse_typed_answer
from app.agents.adzump.tools.campaign_data import (
    ALLOWED_FIELDS, _apply_field, _clear_dependents, _field_traceable,
    _review_hint_if_complete, is_ig_skip,
)
from app.agents.adzump.tools.launch import _launch_campaign

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


# ── F1 · guard hardening ──────────────────────────────────────────────────
class F1GuardTests(unittest.TestCase):
    def test_bare_number_reads_as_days(self):
        self.assertEqual(parse_typed_answer("duration", "30", "$"), "30 days")
        self.assertEqual(parse_typed_answer("duration", "1", "$"), "1 day")
        self.assertEqual(parse_typed_answer("duration", " 45 ", "$"), "45 days")

    def test_budget_bare_number_still_none(self):
        # F1 is duration-only; budget keeps requiring a currency/suffix/per-day.
        self.assertIsNone(parse_typed_answer("budget", "4000", "₹"))

    def test_bare_number_is_traceable_canonically(self):
        # The legit case the deleted fallback used to cover, now canonical.
        self.assertTrue(_field_traceable("duration", "30 days", "30", _ctx()))

    def test_digit_substring_leak_is_closed(self):
        # OLD fallback accepted these (digits ⊂ digits). They must now reject.
        self.assertFalse(_field_traceable("duration", "5 days", "I have 15 properties", _ctx()))
        self.assertFalse(_field_traceable("budget", "₹5,000/day", "call me at 5000", _ctx()))

    def test_existing_canonical_match_preserved(self):
        self.assertTrue(_field_traceable("budget", "₹4,000/day", "4k", _ctx()))
        self.assertFalse(_field_traceable("budget", "₹4,000/day", "no competitors", _ctx()))


# ── F2 · dependency-clear cascade ─────────────────────────────────────────
class F2CascadeTests(unittest.TestCase):
    def test_platform_change_clears_accounts(self):
        sc = _ctx({"platform": "Google Ads", "parent_account": "111", "account": "222"},
                  account_names={"111": "", "222": "", "333": ""})
        sc["_spec_set_at"] = {"platform": 1, "parent_account": 1, "account": 1}
        stored, info = _apply_field("platform", "Meta", "Meta", sc, 2)
        self.assertTrue(stored)
        self.assertEqual(sc["campaign_spec"]["platform"], "Meta")
        self.assertNotIn("parent_account", sc["campaign_spec"])
        self.assertNotIn("account", sc["campaign_spec"])
        self.assertNotIn("account", sc["_spec_set_at"])      # set_at cleared too
        self.assertIn("cleared stale", info)

    def test_cascade_excludes_same_call_siblings(self):
        # Bundled {platform, account}: changing platform must NOT wipe the
        # account being set in the same call.
        sc = _ctx({"platform": "Google Ads", "account": "222"},
                  account_names={"222": "", "999": ""})
        _apply_field("platform", "Meta", "Meta", sc, 2, batch_fields={"platform", "account"})
        self.assertEqual(sc["campaign_spec"].get("account"), "222")   # preserved

    def test_idempotent_resend_does_not_cascade(self):
        sc = _ctx({"platform": "Meta", "account": "222"}, account_names={"222": ""})
        _apply_field("platform", "Meta", "Meta", sc, 2)               # same value
        self.assertEqual(sc["campaign_spec"].get("account"), "222")   # untouched

    def test_first_set_does_not_cascade(self):
        sc = _ctx({"account": "222"}, account_names={"222": ""})
        # platform set for the first time (prior None) → no cascade
        _apply_field("platform", "Meta", "Meta", sc, 2)
        self.assertEqual(sc["campaign_spec"].get("account"), "222")

    def test_fb_page_change_clears_ig(self):
        sc = _ctx({"fb_page": "p1", "ig_page": "i1", "ig_page_declined": "true"},
                  account_names={"p1": "", "p2": "", "i1": ""})
        sc["_ig_offered"] = True
        _apply_field("fb_page", "p2", "p2", sc, 2)
        self.assertNotIn("ig_page", sc["campaign_spec"])
        self.assertNotIn("ig_page_declined", sc["campaign_spec"])
        self.assertNotIn("_ig_offered", sc)                          # F3 marker cleared

    def test_clear_dependents_returns_names(self):
        sc = _ctx({"platform": "Meta", "account": "A"}, account_names={})
        cleared = _clear_dependents("platform", sc, frozenset())
        self.assertIn("account", cleared)


# ── F2 · launch-boundary platform-gate ────────────────────────────────────
class F2LaunchGateTests(unittest.TestCase):
    def _full(self, **over):
        spec = {"platform": "Meta", "duration": "30 days", "budget": "₹5,000/day",
                "parent_account": "G1", "account": "G2"}
        spec.update(over)
        return {"session_context": {"campaign_spec": spec,
                                    "account_platforms": {"G1": "google", "G2": "google"}}}

    def test_cross_platform_id_is_rejected(self):
        res = asyncio.run(_launch_campaign({}, self._full()))
        self.assertFalse(res.success)
        self.assertIn("different platform", res.error)

    def test_matching_platform_passes_gate(self):
        ctx = {"session_context": {
            "campaign_spec": {"platform": "Meta", "duration": "30 days",
                              "budget": "₹5,000/day", "parent_account": "M1", "account": "M2"},
            "account_platforms": {"M1": "meta", "M2": "meta"}}}
        with mock.patch("app.agents.adzump.tools.launch.save_campaign",
                        new=mock.AsyncMock(return_value="rec_123")):
            res = asyncio.run(_launch_campaign({}, ctx))
        self.assertTrue(res.success)                                 # gate let it through

    def test_untagged_ids_skip_gate_backcompat(self):
        # Old session with no account_platforms map → no false reject.
        ctx = {"session_context": {
            "campaign_spec": {"platform": "Meta", "duration": "30 days",
                              "budget": "₹5,000/day", "parent_account": "X1", "account": "X2"}}}
        with mock.patch("app.agents.adzump.tools.launch.save_campaign",
                        new=mock.AsyncMock(return_value="rec_456")):
            res = asyncio.run(_launch_campaign({}, ctx))
        self.assertTrue(res.success)


# ── F3 · Instagram optional ────────────────────────────────────────────────
class F3IgOptionalTests(unittest.TestCase):
    META_FULL = {"platform": "Meta", "duration": "30 days", "budget": "$50/day",
                 "parent_account": "P", "account": "A", "fb_page": "F"}

    def test_ig_page_declined_is_allowed_field(self):
        self.assertIn("ig_page_declined", ALLOWED_FIELDS)

    def test_is_ig_skip_recognizes_optouts(self):
        for s in ("skip insta page", "lets do it later", "facebook only",
                  "continue with facebook only", "no instagram", "skip"):
            self.assertTrue(is_ig_skip(s), s)
        for s in ("link instagram", "yes please", "proceed", "pick the first one"):
            self.assertFalse(is_ig_skip(s), s)

    def test_decline_flag_traceable(self):
        self.assertTrue(_field_traceable("ig_page_declined", "true", "Continue with Facebook only", _ctx()))
        self.assertTrue(_field_traceable("ig_page_declined", "true", "skip insta", _ctx()))
        self.assertFalse(_field_traceable("ig_page_declined", "true", "yes link instagram", _ctx()))

    def test_next_action_offers_ig_once(self):
        m = _next_action(_cctx(dict(self.META_FULL)))
        self.assertTrue(any("fetch_meta_ig_accounts" in x for x in m))

    def test_next_action_skip_cue_declines(self):
        m = _next_action(_cctx(dict(self.META_FULL), last_user="skip insta page"))
        self.assertTrue(any("ig_page_declined" in x for x in m))
        self.assertFalse(any("fetch_meta_ig_accounts" in x for x in m))

    def test_next_action_offered_does_not_refetch(self):
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
        spec = {**self.META_FULL, "ig_page_declined": "true"}
        m = _next_action(_cctx(spec))
        self.assertTrue(any("review" in x.lower() for x in m))
        self.assertFalse(any("instagram" in x.lower() and "fetch" in x.lower() for x in m))

    def test_review_gate_waits_until_ig_offered(self):
        # fb_page set but IG neither picked nor declined → not complete yet
        self.assertEqual(_review_hint_if_complete(dict(self.META_FULL), {"product_data": SAAS}), "")

    def test_review_gate_complete_on_facebook_only(self):
        spec = {**self.META_FULL, "ig_page_declined": "true"}
        hint = _review_hint_if_complete(spec, {"product_data": SAAS})
        self.assertNotEqual(hint, "")
        self.assertIn("not linked (Facebook only)", hint)


# ── F4 · reliable post-capture ask ─────────────────────────────────────────
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


class F4CaptureMarkerTests(unittest.TestCase):
    def test_marker_set_on_capture(self):
        s = _session(_dur_pe(), "30 days")
        AdzumpAgent._capture_tagged_answer(None, s, turn=1)
        self.assertEqual(s.context.get("_captured_this_turn"), "duration")

    def test_suppresses_infer_when_captured(self):
        s = types.SimpleNamespace(); s.context = {"_captured_this_turn": "duration"}
        sentinel = mock.AsyncMock(return_value={"options": [], "mode": "single"})
        with mock.patch("app.agents.adzump.agent.infer_suggestions", new=sentinel):
            res = asyncio.run(AdzumpAgent.get_pending_suggestions(None, s, "How long should it run?"))
        self.assertIsNone(res)                                   # suppressed
        sentinel.assert_not_awaited()                            # infer never reached
        self.assertNotIn("_captured_this_turn", s.context)       # popped

    def test_no_marker_reaches_infer(self):
        # Over-suppress control: without the marker, infer IS reached.
        s = types.SimpleNamespace(); s.context = {}
        sentinel = mock.AsyncMock(return_value={"options": [1], "mode": "single"})
        with mock.patch("app.agents.adzump.agent.infer_suggestions", new=sentinel):
            res = asyncio.run(AdzumpAgent.get_pending_suggestions(None, s, "How long?"))
        sentinel.assert_awaited_once()
        self.assertEqual(res, {"options": [1], "mode": "single"})

    def test_marker_popped_even_on_early_return(self):
        # Leak invariant: explicit chips short-circuit, but the marker is popped
        # up-front so it can't survive to the next turn.
        s = types.SimpleNamespace()
        s.context = {"_captured_this_turn": "duration",
                     "_pending_suggestions": {"options": [{"label": "x", "value": "x"}], "mode": "single"}}
        res = asyncio.run(AdzumpAgent.get_pending_suggestions(None, s, ""))
        self.assertEqual(res["options"][0]["value"], "x")        # explicit chips returned
        self.assertNotIn("_captured_this_turn", s.context)       # still popped — no leak


if __name__ == "__main__":
    unittest.main()
