"""campaign_data below-the-model mechanics: is_decline, the _apply_field
dependency cascade, _set_campaign_spec (invention breaker, kept-noop, leak
containment), and clear_competitor_decline. Traceability + parse tables live
in tests/agents/adzump/test_answer_capture.py.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump.tools.test_campaign_data -v
"""
import asyncio
import unittest

from app.agents.adzump.tools.campaign_data import (
    _apply_field, _clear_dependents, _set_campaign_spec,
    clear_competitor_decline, is_clear_decline_reply, is_decline, is_real_estate,
)
from app.agents.adzump.services.business_storage import _build_full_record
from tests.agents.adzump._fixtures import RE, spec_context


class IsDeclineTests(unittest.TestCase):
    def test_table(self):
        declines = [
            "No, skip competitor analysis for now",  # the live F11 phrase (comma!)
            "no", "n", "No", "skip", "skip it", "not now", "no need", "no thanks",
            "maybe later", "don't bother",
        ]
        not_declines = [  # polarity-flips: 'no' rejects something ELSE, not the offer
            "no, change the budget to 20k",
            "no, that competitor is wrong",
            "yes", "go ahead, analyze them", "analyze competitors", "",
        ]
        for text, expected in [(t, True) for t in declines] + \
                              [(t, False) for t in not_declines]:
            with self.subTest(text=text):
                self.assertEqual(bool(is_decline(text)), expected)


class InventionRetryLoopTests(unittest.TestCase):
    # regression: F12 - decline→invent values→retry-with-fresh-values evaded the v5 breaker
    def test_invented_fields_after_decline_store_nothing_and_steer_ask(self):
        ctx, sc = spec_context({}, "No, skip competitor analysis for now")
        r = asyncio.run(_set_campaign_spec({"duration": "30 days", "budget": "₹5,000/day"}, ctx))
        self.assertFalse(r.success)
        self.assertNotIn("duration", sc["campaign_spec"])   # nothing invented stored
        self.assertNotIn("budget", sc["campaign_spec"])
        self.assertIn("ask", (r.error or "").lower())       # steer = ASK, not retry

    def test_breaker_fires_on_field_set_despite_differing_values(self):
        ctx, sc = spec_context({}, "no, skip competitors")
        invented = [("30 days", "₹5,000/day"), ("60 days", "₹6,000/day"),
                    ("45 days", "₹8,000/day")]
        last = None
        for dur, bud in invented:
            last = asyncio.run(_set_campaign_spec({"duration": dur, "budget": bud}, ctx))
            self.assertFalse(last.success)
        self.assertIn("STOP", last.error or "")  # fires on 3rd despite different values

    def test_kept_noop_emits_no_progress(self):  # F15: paraphrase of a stored field
        full = ("302, Blk 9, Cityville Valmark, off Bannerghatta Rd, "
                "Bengaluru, Karnataka 560076, India")
        ctx, sc = spec_context({"location": full}, "continue")
        r = asyncio.run(_set_campaign_spec({"location": "Bengaluru"}, ctx))
        self.assertTrue(r.success)
        self.assertTrue(isinstance(r.data, dict) and r.data.get("no_progress"))

    def test_legit_varied_correction_does_not_trip_breaker(self):
        ctx, sc = spec_context({}, "make it 30 days")
        r = asyncio.run(_set_campaign_spec({"duration": "30 days"}, ctx))
        self.assertTrue(r.success)
        self.assertEqual(sc["campaign_spec"]["duration"], "30 days")
        self.assertIsNone(ctx["session_context"].get("_spec_reject_streak"))


# ── F2 · dependency-clear cascade ─────────────────────────────────────────
def _ctx(spec=None, **extra):
    c = {"product_data": dict(RE), "campaign_spec": dict(spec or {}), "_spec_set_at": {}}
    c.update(extra)
    return c


class DependencyCascadeTests(unittest.TestCase):
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

    def test_no_cascade_on_resend_or_first_set(self):
        # idempotent re-send of the same value → untouched
        sc = _ctx({"platform": "Meta", "account": "222"}, account_names={"222": ""})
        _apply_field("platform", "Meta", "Meta", sc, 2)
        self.assertEqual(sc["campaign_spec"].get("account"), "222")
        # platform set for the first time (prior None) → no cascade
        sc = _ctx({"account": "222"}, account_names={"222": ""})
        _apply_field("platform", "Meta", "Meta", sc, 2)
        self.assertEqual(sc["campaign_spec"].get("account"), "222")

    def test_any_edit_reopens_the_launched_draft(self):
        # regression: campaign_status had one writer (launch) and zero clearers,
        # so edit-budget-then-relaunch was refused forever. Any successful spec
        # write must pop it - relaunch then re-asks consent through the gate.
        sc = _ctx({"platform": "Google Ads", "budget": "₹5,000/day",
                   "campaign_status": "launched"})
        stored, _ = _apply_field("budget", "₹10,000/day", "₹10,000/day", sc, 5)
        self.assertTrue(stored)
        self.assertNotIn("campaign_status", sc["campaign_spec"])

    def test_rejected_write_keeps_launched_status(self):
        # an untraceable value stores nothing - the launch lock must survive.
        sc = _ctx({"platform": "Google Ads", "campaign_status": "launched"})
        stored, _ = _apply_field("budget", "₹9,999/day", "unrelated message", sc, 5)
        self.assertFalse(stored)
        self.assertEqual(sc["campaign_spec"].get("campaign_status"), "launched")

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

    def test_platform_change_voids_creatives_offered_marker(self):
        sc = _ctx({"platform": "Meta"})
        sc["_competitor_creatives_offered"] = True
        _clear_dependents("platform", sc, frozenset())
        self.assertNotIn("_competitor_creatives_offered", sc)
        # A downstream change (fb_page) leaves the offer marker alone.
        sc["_competitor_creatives_offered"] = True
        _clear_dependents("fb_page", sc, frozenset())
        self.assertIn("_competitor_creatives_offered", sc)


# ── v5 · set_campaign_spec retry-loop fixes ────────────────────────────────
# Live bug (2026-06-10, cityville run): the model re-sent the whole spec with
# the stored location paraphrased ("Bengaluru" ≠ stored full address), the
# provenance guard rejected it as an ERROR, and the model retried the same
# call 25+ times. Fixes: (1) untraceable re-send of an ALREADY-STORED field is
# a kept no-op, not an error; (2) 3 identical all-rejected calls escalate to a
# hard STOP steer; (3) an unknown ig_page id hints the ig_page_declined key.
FULL_ADDR = ("302, Blk 9, Cityville Valmark, off Bannerghatta Rd, "
             "Bengaluru, Karnataka 560076, India")


class SpecRetryBreakerTests(unittest.TestCase):
    def test_paraphrase_of_stored_field_is_kept_not_error(self):
        ctx, sc = spec_context({"location": FULL_ADDR}, "continue")
        r = asyncio.run(_set_campaign_spec({"location": "Bengaluru"}, ctx))
        self.assertTrue(r.success)
        self.assertIn("kept", (r.model_summary or ""))      # steer is model-only now
        self.assertIn("re-send", (r.model_summary or ""))
        self.assertNotIn("kept", (r.summary or ""))         # user/card never sees the steer
        self.assertEqual(sc["campaign_spec"]["location"], FULL_ADDR)

    def test_empty_field_untraceable_still_rejected(self):
        ctx, sc = spec_context({}, "continue")
        r = asyncio.run(_set_campaign_spec({"location": "Bengaluru"}, ctx))
        self.assertFalse(r.success)
        self.assertNotIn("location", sc["campaign_spec"])

    def test_third_identical_rejection_escalates_to_stop(self):
        ctx, sc = spec_context({}, "continue")
        for _ in range(2):
            r = asyncio.run(_set_campaign_spec({"location": "Bengaluru"}, ctx))
            self.assertFalse(r.success)
            self.assertNotIn("STOP", r.error or "")
        r = asyncio.run(_set_campaign_spec({"location": "Bengaluru"}, ctx))
        self.assertFalse(r.success)
        self.assertIn("STOP", r.error or "")

    def test_streak_resets_on_progress(self):
        ctx, sc = spec_context({}, "continue")
        for _ in range(2):
            asyncio.run(_set_campaign_spec({"location": "Bengaluru"}, ctx))
        ctx["_session"].messages = [{"role": "user", "content": "90 days"}]
        r = asyncio.run(_set_campaign_spec({"duration": "90 days"}, ctx))
        self.assertTrue(r.success)
        ctx["_session"].messages = [{"role": "user", "content": "continue"}]
        r = asyncio.run(_set_campaign_spec({"location": "Bengaluru"}, ctx))
        self.assertNotIn("STOP", r.error or "")

    def test_ig_page_rejection_hints_declined_key(self):
        ctx, sc = spec_context({}, "continue")
        r = asyncio.run(_set_campaign_spec({"ig_page": "true"}, ctx))
        self.assertFalse(r.success)
        self.assertIn("ig_page_declined", r.error or "")

    def test_stored_account_field_unknown_id_still_rejected(self):
        # Kiran (v5 review): the kept-noop must NOT swallow account fields -
        # a different unknown id on a stored account is an attempted switch
        # and stays an actionable rejection (re-fetch), never a silent keep.
        ctx, sc = spec_context({"account": "act_111"}, "switch to act_999")
        r = asyncio.run(_set_campaign_spec({"account": "act_999"}, ctx))
        self.assertFalse(r.success)
        self.assertIn("fetch", r.error or "")
        self.assertEqual(sc["campaign_spec"]["account"], "act_111")

    def test_stored_ig_page_unknown_value_keeps_hint(self):
        # With ig_page already stored, the ig_page_declined hint must still
        # surface (kept-noop would have swallowed it before the narrowing).
        ctx, sc = spec_context({"ig_page": "12345"}, "continue")
        r = asyncio.run(_set_campaign_spec({"ig_page": "true"}, ctx))
        self.assertFalse(r.success)
        self.assertIn("ig_page_declined", r.error or "")
        self.assertEqual(sc["campaign_spec"]["ig_page"], "12345")


# ── F17c · the breaker blind spot: partial that stores nothing ─────────────
ADDR = "3J8G+23, Rachenahalli, Thanisandra, Bengaluru, Karnataka 560045, India"


class NoProgressFloorTests(unittest.TestCase):
    def test_kept_plus_rejected_storing_nothing_flags_no_progress(self):
        # exact F17c shape: location kept (paraphrase of stored), duration="true"
        # rejected (invented). Nothing NEW stored → must flag no_progress so the
        # stuck-step breaker counts it (this is what looped 18×).
        ctx, sc = spec_context({"location": ADDR}, "")
        r = asyncio.run(_set_campaign_spec(
            {"location": "Bengaluru", "duration": "true"}, ctx))
        self.assertTrue(r.success)                                  # partial = success
        self.assertTrue(isinstance(r.data, dict) and r.data.get("no_progress"))
        self.assertNotIn("duration", sc["campaign_spec"])           # "true" not stored

    def test_partial_that_stores_something_is_not_no_progress(self):
        # boundary: a real store + a rejected invent is genuine progress → no flag,
        # so a legit correction bundled with a stray field never trips the breaker.
        ctx, sc = spec_context({}, "make it 30 days")
        r = asyncio.run(_set_campaign_spec(
            {"duration": "30 days", "budget": "true"}, ctx))
        self.assertTrue(r.success)
        self.assertEqual(sc["campaign_spec"]["duration"], "30 days")
        self.assertFalse(isinstance(r.data, dict) and r.data.get("no_progress"))


# ── validator rejections must NOT leak into the user-facing summary ──
# Seen live (dev, 2026-06-24): "rejected platform=Google Ads (not traceable…)"
# rendered in the activity card. The steer is model-only now (model_summary on
# success / error on failure); the user-facing `summary` carries only what was
# actually stored.
class ValidatorLeakContainmentTests(unittest.TestCase):
    _LEAKS = ("rejected", "not traceable", "cannot set", "=")  # internal steer markers

    def _assert_clean(self, summary):
        s = (summary or "").lower()
        for leak in self._LEAKS:
            self.assertNotIn(leak, s, f"validator steer leaked into user summary: {leak!r} in {summary!r}")

    def test_partial_reject_summary_clean_steer_model_only(self):
        # stores duration (traceable), rejects budget="true" (invented) → partial
        ctx, sc = spec_context({}, "make it 30 days")
        r = asyncio.run(_set_campaign_spec({"duration": "30 days", "budget": "true"}, ctx))
        self.assertTrue(r.success)
        self.assertEqual(sc["campaign_spec"]["duration"], "30 days")
        self.assertNotIn("budget", sc["campaign_spec"])          # rejected, not stored
        self._assert_clean(r.summary)                            # user/card: clean
        self.assertIn("rejected", r.to_tool_result_content().lower())  # model: still steered

    def test_all_rejected_summary_clean_steer_in_error(self):
        ctx, sc = spec_context({}, "continue")
        r = asyncio.run(_set_campaign_spec({"platform": "Google Ads"}, ctx))
        self.assertFalse(r.success)
        self._assert_clean(r.summary)                            # user/card: clean
        self.assertIn("traceable", (r.error or "").lower())      # model: steer in error


# ── F17a · bleed containment: only the traceable declined field lands ──
class BleedContainmentTests(unittest.TestCase):
    def test_decline_bleed_stores_only_the_declined_field(self):
        ctx, sc = spec_context({"platform": "Google Ads"}, "no thanks, skip it")
        r = asyncio.run(_set_campaign_spec({
            "competitive_analysis_declined": "true", "duration": "true",
            "budget": "true", "account": "true",
        }, ctx))
        self.assertTrue(r.success)
        self.assertEqual(sc["campaign_spec"].get("competitive_analysis_declined"), "true")
        for f in ("duration", "budget", "account"):
            self.assertNotIn(f, sc["campaign_spec"])               # bleed contained


# ── F17b · record the decline deterministically (chip + tight typed) ──
class ClearDeclineReplyTableTests(unittest.TestCase):
    def test_table(self):
        clear = ["no", "n", "no thanks", "no thanks, skip it", "skip it",
                 "No, skip competitor analysis", "not now", "maybe later", "no need"]
        ambiguous = ["no competitors named yet", "not now, first tell me about the audience",
                     "no, make it Meta", "what about competitors?", "no - which ones?",
                     "skip - but tell me how it works"]
        for text, expected in [(t, True) for t in clear] + \
                              [(t, False) for t in ambiguous]:
            with self.subTest(text=text):
                self.assertEqual(bool(is_clear_decline_reply(text)), expected)


class ClearAffirmativeReplyTableTests(unittest.TestCase):
    """The shared yes-core behind the launch + competitor-creatives gates."""

    def test_table(self):
        from app.agents.adzump.tools.campaign_data import is_clear_affirmative_reply
        cases = [
            ("yes", True), ("YES", True), ("yes, show me", True),
            ("go ahead", True), ("sure, do it", True), ("okay", True),
            ("", False),
            ("yesterday we discussed eyes", False),   # word boundary
            ("what budget did we pick?", False),      # question, no go-ahead
            ("no thanks", False),                     # clear decline wins
            ("not now, maybe later", False),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(is_clear_affirmative_reply(text), expected)

    def test_creatives_decline_flag_traceability(self):
        from app.agents.adzump.tools.campaign_data import _field_traceable
        ctx = {"product_data": dict(RE), "campaign_spec": {}, "_spec_set_at": {}}
        for user, expected in [("No", True), ("no thanks", True), ("yes please", False)]:
            with self.subTest(user=user):
                self.assertEqual(
                    _field_traceable("competitor_creatives_declined", "true", user, ctx),
                    expected)


class CreativesOfferResolvedTests(unittest.TestCase):
    """The ONE predicate behind _next_action's offer gate and the review gate."""

    def test_table(self):
        from app.agents.adzump.tools.campaign_data import (
            competitor_creatives_offer_resolved,
        )
        rival = {"name": "R", "url": "https://r.com"}
        cases = [
            ("declined", {"competitor_creatives_declined": "true"}, {}, True),
            ("analysis itself declined",
             {"competitive_analysis_declined": "true"}, {}, True),
            ("fetch completed, zero ads", {},
             {"_competitor_creatives_fetched": True,
              "competitor_analysis": {"competitors": [dict(rival)]}}, True),
            ("creatives attached (pre-marker session)", {},
             {"competitor_analysis": {"competitors": [
                 {**rival, "creatives": [{"creativeId": "1"}]}]}}, True),
            ("moot: analysis ran, no named rivals", {},
             {"competitor_analysis": {"competitors": [{"url": "https://x.com"}]}},
             True),
            ("unresolved: rivals found, no consent yet", {},
             {"competitor_analysis": {"competitors": [dict(rival)]}}, False),
            ("unresolved: no analysis yet", {}, {}, False),
        ]
        for name, spec, session_ctx, expected in cases:
            with self.subTest(case=name):
                self.assertEqual(
                    competitor_creatives_offer_resolved(spec, session_ctx),
                    expected)


# ── F26 · clear_competitor_decline + durable-record consistency ────────────
class WantsCompetitorCreativesTests(unittest.TestCase):
    """The ONE consent predicate behind fetch_competitor_creatives' hard gate
    and _next_action's said-yes prescription - they must never disagree."""

    def test_table(self):
        from app.agents.adzump.tools.campaign_data import wants_competitor_creatives
        cases = [
            ("Yes", True), ("yes, go ahead", True), ("sure", True),
            ("show me their ads", True), ("let's see the creatives", True),
            ("fetch their ads please", True),
            ("", False),
            ("no thanks", False),                           # clear decline wins
            ("what will this cost me?", False),             # question, no consent
            ("show me the budget options", False),          # verb without ad noun
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(wants_competitor_creatives(text), expected)


class ClearHelperTests(unittest.TestCase):
    def test_pops_flag_and_provenance(self):
        sc = {"campaign_spec": {"platform": "Google Ads",
                                "competitive_analysis_declined": "true"},
              "_spec_set_at": {"competitive_analysis_declined": 3, "platform": 1}}
        self.assertTrue(clear_competitor_decline(sc))
        self.assertNotIn("competitive_analysis_declined", sc["campaign_spec"])
        self.assertNotIn("competitive_analysis_declined", sc["_spec_set_at"])
        self.assertIn("platform", sc["campaign_spec"])            # untouched
        self.assertIn("platform", sc["_spec_set_at"])

    def test_idempotent_and_missing_dicts_safe(self):
        sc = {"campaign_spec": {"platform": "Google Ads"}, "_spec_set_at": {}}
        self.assertFalse(clear_competitor_decline(sc))            # flag absent → noop
        self.assertEqual(sc["campaign_spec"], {"platform": "Google Ads"})
        self.assertFalse(clear_competitor_decline({}))            # no crash


class PostClearConsistencyTests(unittest.TestCase):
    def test_clear_then_record_is_consistent(self):
        # simulate the tool path: contradiction state → clear → build record.
        sc = {"product_data": dict(RE),
              "campaign_spec": {"platform": "Google Ads",
                                "competitive_analysis_declined": "true"},
              "_spec_set_at": {"competitive_analysis_declined": 4},
              "competitor_analysis": {"competitors": [{"name": "Prestige"}]}}
        self.assertTrue(clear_competitor_decline(sc))
        c = _build_full_record(sc, "https://example.com")["campaign"]["competitive"]
        self.assertTrue(c["attempted"])
        self.assertFalse(c["declined"])
        self.assertNotIn("competitive_analysis_declined", sc["campaign_spec"])


class IsRealEstateTests(unittest.TestCase):
    """Gates the real-estate conditional (our first vertical)."""

    def test_table(self):
        for bt, expected in [
            ("Real Estate Developer", True), ("Luxury Villas", True),
            ("3BHK Apartments", True), ("Residential Township", True),
            ("Property Management", True), ("realty group", True),
            ("SaaS platform", False), ("Restaurant chain", False),
            ("Law firm", False), ("", False), (None, False),
        ]:
            with self.subTest(bt=bt):
                self.assertEqual(bool(is_real_estate(bt)), expected)


if __name__ == "__main__":
    unittest.main()
