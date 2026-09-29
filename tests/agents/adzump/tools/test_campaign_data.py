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
    _apply_field, _set_campaign_spec,
    clear_competitor_decline, is_clear_decline_reply, is_decline, is_real_estate,
)
from tests.agents.adzump._fixtures import RE, make_session, spec_context


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
    def test_invented_values_store_nothing_and_the_breaker_fires(self):
        ctx, sc = spec_context({}, "No, skip competitor analysis for now")
        invented = [("30 days", "₹5,000/day"), ("60 days", "₹6,000/day"),
                    ("45 days", "₹8,000/day")]
        for i, (dur, bud) in enumerate(invented):
            r = asyncio.run(_set_campaign_spec({"duration": dur, "budget": bud}, ctx))
            self.assertFalse(r.success)
            self.assertNotIn("duration", sc["campaign_spec"])   # nothing invented stored
            self.assertNotIn("budget", sc["campaign_spec"])
            if i == 0:
                self.assertIn("ask", (r.error or "").lower())   # steer = ASK, not retry
        self.assertIn("STOP", r.error or "")  # fires on 3rd despite different values


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
        sc["ig_accounts"] = ["i1"]
        sc["_field_asks"] = {"instagram": 1, "competitor_creatives": 1}
        _apply_field("fb_page", "p2", "p2", sc, 2)
        self.assertNotIn("ig_page", sc["campaign_spec"])
        self.assertNotIn("ig_page_declined", sc["campaign_spec"])
        self.assertNotIn("ig_accounts", sc)                # F3 fetched list cleared
        self.assertEqual(sc["_field_asks"], {"competitor_creatives": 1})

    def test_platform_change_resets_enum_offers(self):
        # Offers reset to UNSET (key popped) on a platform switch - a Google
        # decline must not silently carry into the Meta flow - and their ask
        # counts go with them.
        sc = _ctx({"platform": "Google Ads", "competitive_analysis": "accepted",
                   "competitor_creatives": "declined", "instagram": "declined"})
        sc["_field_asks"] = {"competitor_creatives": 2}
        _apply_field("platform", "Meta", "Meta", sc, 2)
        for offer in ("competitive_analysis", "competitor_creatives", "instagram"):
            self.assertNotIn(offer, sc["campaign_spec"])
        self.assertNotIn("_field_asks", sc)

    def test_location_change_clears_target_areas(self):
        # S1-9/R11 - a corrected city must never launch on the old polygons.
        sc = _ctx({"platform": "Google Ads", "location": "Pune"})
        sc["product_data"] = {"business_type": "real estate",
                              "target_areas": [{"name": "Pune", "google": {"id": 1}}]}
        stored, info = _apply_field("location", "Mumbai", "make it Mumbai", sc, 3)
        self.assertTrue(stored)
        self.assertNotIn("target_areas", sc["product_data"])
        self.assertIn("target_areas", info)
        # First-set never cascades: a fresh confirm keeps existing targets.
        sc2 = _ctx({})
        sc2["product_data"] = {"target_areas": [{"name": "Pune"}]}
        _apply_field("location", "Pune", "Pune", sc2, 1)
        self.assertIn("target_areas", sc2["product_data"])



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
        self.assertTrue(isinstance(r.data, dict) and r.data.get("no_progress"))  # F15

    def test_third_identical_rejection_escalates_to_stop(self):
        ctx, sc = spec_context({}, "continue")
        for _ in range(2):
            r = asyncio.run(_set_campaign_spec({"location": "Bengaluru"}, ctx))
            self.assertFalse(r.success)
            self.assertNotIn("STOP", r.error or "")
            self.assertNotIn("location", sc["campaign_spec"])  # an empty field stays empty
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
        # With ig_page already stored the Facebook-only hint must still surface
        # (the kept-noop would have swallowed it before the narrowing).
        for label, spec in [("nothing stored", {}), ("ig_page stored", {"ig_page": "12345"})]:
            with self.subTest(label):
                ctx, sc = spec_context(dict(spec), "continue")
                r = asyncio.run(_set_campaign_spec({"ig_page": "true"}, ctx))
                self.assertFalse(r.success)
                self.assertIn('instagram="declined"', r.error or "")
                self.assertEqual(sc["campaign_spec"].get("ig_page"), spec.get("ig_page"))

    def test_stored_account_field_unknown_id_still_rejected(self):
        # Kiran (v5 review): the kept-noop must NOT swallow account fields -
        # a different unknown id on a stored account is an attempted switch
        # and stays an actionable rejection (re-fetch), never a silent keep.
        ctx, sc = spec_context({"account": "act_111"}, "switch to act_999")
        r = asyncio.run(_set_campaign_spec({"account": "act_999"}, ctx))
        self.assertFalse(r.success)
        self.assertIn("fetch", r.error or "")
        self.assertEqual(sc["campaign_spec"]["account"], "act_111")


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
        # A real store beside a rejected invent is progress: never trips the breaker.
        self.assertFalse(isinstance(r.data, dict) and r.data.get("no_progress"))
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
        # Legacy write canonicalizes to the enum at the _apply_field seam.
        self.assertEqual(sc["campaign_spec"].get("competitive_analysis"), "declined")
        self.assertNotIn("competitive_analysis_declined", sc["campaign_spec"])
        for f in ("duration", "budget", "account"):
            self.assertNotIn(f, sc["campaign_spec"])               # bleed contained


# ── F17b · record the decline deterministically (chip + tight typed) ──
class ClearDeclineReplyTableTests(unittest.TestCase):
    def test_table(self):
        clear = ["no", "n", "no thanks", "no thanks, skip it", "skip it",
                 "No, skip competitor analysis", "not now", "maybe later", "no need"]
        ambiguous = ["no competitors named yet", "not now, first tell me about the audience",
                     "no, make it Meta", "what about competitors?", "no - which ones?",
                     "skip - but tell me how it works",
                     "👍", "🤔", "👍 sounds good", "   "]  # an emoji is not a decline
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


class CreativesOfferResolutionTests(unittest.TestCase):
    """The ONE typed verdict behind the creatives step's offer gate, the review
    gate, and the turn record - resolution WITH its reason (slice 4)."""

    def test_table(self):
        from app.agents.adzump.models import OfferResolution as R
        from app.agents.adzump.tools.campaign_data import (
            creatives_offer_resolution,
        )
        rival = {"name": "R", "url": "https://r.com"}
        cases = [
            ("declined", {"competitor_creatives_declined": "true"}, {}, R.DECLINED),
            ("declined (enum)", {"competitor_creatives": "declined"}, {}, R.DECLINED),
            ("analysis itself declined",
             {"competitive_analysis_declined": "true"}, {}, R.DECLINED),
            ("analysis itself declined (enum)",
             {"competitive_analysis": "declined"}, {}, R.DECLINED),
            ("accepted alone is NOT resolved (fetch still owed)",
             {"competitor_creatives": "accepted"},
             {"competitor_analysis": {"competitors": [
                 {"name": "R", "url": "https://r.com"}]}}, R.OPEN),
            ("fetch completed, zero ads (fetched-empty covers)", {},
             {"competitor_analysis": {"competitors": [
                 {**rival, "creatives": []}]}}, R.FULFILLED),
            ("creatives attached", {},
             {"competitor_analysis": {"competitors": [
                 {**rival, "creatives": [{"creativeId": "1"}]}]}}, R.FULFILLED),
            # Coverage, not a one-shot latch: a competitor added AFTER the
            # fetch re-opens the offer (live 2026-09-11: post-fetch adds were
            # railroaded straight to the duration question).
            ("competitor added after the fetch re-opens", {},
             {"competitor_analysis": {"competitors": [
                 {**rival, "creatives": []},
                 {"name": "Newcomer", "url": "https://new.com"}]}}, R.OPEN),
            ("moot: analysis ran, no named rivals", {},
             {"competitor_analysis": {"competitors": [{"url": "https://x.com"}]}},
             R.MOOT),
            ("unresolved: rivals found, no consent yet", {},
             {"competitor_analysis": {"competitors": [dict(rival)]}}, R.OPEN),
            ("exhausted: asked twice, never answered", {},
             {"_field_asks": {"competitor_creatives": 2},
              "competitor_analysis": {"competitors": [dict(rival)]}}, R.EXHAUSTED),
            ("asked once is NOT exhausted", {},
             {"_field_asks": {"competitor_creatives": 1},
              "competitor_analysis": {"competitors": [dict(rival)]}}, R.OPEN),
            ("unresolved: no analysis yet", {}, {}, R.OPEN),
        ]
        for name, spec, session_ctx, expected in cases:
            with self.subTest(case=name):
                self.assertIs(
                    creatives_offer_resolution(spec, session_ctx), expected)


# ── F26 · clear_competitor_decline + durable-record consistency ────────────
class AdAccountsReuseTests(unittest.TestCase):
    """Account picks are the business's: every pick is remembered on the product
    per platform, and choosing that platform again reuses them - budget and
    duration are asked fresh (Kailash 2026-09-23)."""

    META_NAMES = {"B1": "AdZump Dummy", "A1": "Main ad account", "P1": "Misty Shores"}

    def _write(self, ctx, field, value, user, batch=frozenset()):
        from app.agents.adzump.tools.campaign_data import _apply_field
        return _apply_field(field, value, user, ctx, 1, batch)

    def test_picks_are_remembered_per_platform(self):
        ctx = {"campaign_spec": {"platform": "Meta"}, "account_names": dict(self.META_NAMES)}
        for field, acct in (("parent_account", "B1"), ("account", "A1"), ("fb_page", "P1")):
            stored, _ = self._write(ctx, field, acct, acct)
            self.assertTrue(stored, field)
        self._write(ctx, "instagram", "declined", "skip instagram")
        saved = ctx["product_data"]["ad_accounts"]["meta"]
        self.assertEqual((saved["parent_account"], saved["account"], saved["fb_page"]),
                         ("B1", "A1", "P1"))
        self.assertEqual(saved["names"], self.META_NAMES)
        self.assertEqual(saved["instagram"], "declined")

    def test_platform_choice_reuses_saved_picks(self):
        saved = {"meta": {"parent_account": "B1", "account": "A1", "fb_page": "P1",
                          "instagram": "declined", "names": self.META_NAMES}}
        rows = [  # (case, platform, batch, expected spec account, reused note?)
            ("same platform reuses", "Meta", frozenset(), "A1", True),
            ("other platform reuses nothing", "Google Ads", frozenset(), None, False),
            ("a pick in the same write wins", "Meta", frozenset({"account"}), None, True),
        ]
        for case, platform, batch, account, noted in rows:
            with self.subTest(case):
                ctx = {"campaign_spec": {}, "product_data": {"ad_accounts": saved}}
                stored, info = self._write(ctx, "platform", platform, platform, batch)
                spec = ctx["campaign_spec"]
                self.assertTrue(stored)
                self.assertEqual(spec.get("account"), account)
                self.assertEqual("reused saved accounts" in info, noted)
                if platform == "Meta":
                    self.assertEqual(spec["parent_account"], "B1")
                    self.assertEqual(spec["instagram"], "declined")
                    self.assertEqual(ctx["account_names"]["B1"], "AdZump Dummy")


class ProductChangesTests(unittest.TestCase):
    """A spec answer that changes the product (a location, an account) is
    saved on the product row as just those fields, and the model is told."""

    def test_product_changes_for(self):
        from app.agents.adzump.tools.campaign_data import product_changes_for
        meta = {"account": "A1"}
        ctx = {"campaign_spec": {"platform": "Meta"},
               "product_data": {"place": {"address": "Hebbal"}, "ad_accounts": {"meta": meta}}}
        rows = [  # (case, field, changes)
            ("location: place and the cleared areas", "location",
             {"place": {"address": "Hebbal"}, "target_areas": []}),
            ("an account: that platform's accounts", "account", {"ad_accounts.meta": meta}),
            ("instagram decline", "instagram", {"ad_accounts.meta": meta}),
            ("legacy instagram marker", "ig_page_declined", {"ad_accounts.meta": meta}),
            ("budget: nothing on the product", "budget", {}),
        ]
        for case, field, changes in rows:
            with self.subTest(case):
                self.assertEqual(product_changes_for(field, ctx), changes)
        no_platform = {"campaign_spec": {}, "product_data": ctx["product_data"]}
        self.assertEqual(product_changes_for("account", no_platform), {})

    def test_save_product_changes_tells_the_model(self):
        from unittest import mock
        from app.agents.adzump.tools.campaign_data import save_product_changes
        ctx = {"campaign_spec": {"platform": "Meta"},
               "product_data": {"ad_accounts": {"meta": {"account": "A1"}}}}
        rows = [  # (case, fields, save outcome, note contains)
            ("saved", ["account"], True, "Saved on the product"),
            ("no product row", ["account"], False, "failed"),
            ("database error", ["account"], RuntimeError("down"), "failed"),
            ("nothing on the product", ["budget"], True, None),
        ]
        for case, fields, outcome, note in rows:
            with self.subTest(case):
                save = mock.AsyncMock(side_effect=outcome if isinstance(outcome, Exception)
                                      else None, return_value=outcome)
                with mock.patch("app.agents.adzump.services.product_service.save_product_fields",
                                new=save):
                    result = asyncio.run(save_product_changes(fields, ctx, {}))
                if note is None:
                    self.assertEqual(result, "")
                    save.assert_not_awaited()
                else:
                    self.assertIn(note, result)
                    self.assertEqual(save.await_args.args[2], {"ad_accounts.meta": {"account": "A1"}})


class StoreConfirmedLocationTests(unittest.TestCase):
    """A pin confirmed where the backend put it keeps the detected address; a
    moved pin takes the map's street address (live 2026-09-23: an untouched
    pin renamed "Near ITPB (Whitefield)" to a street nobody picked)."""

    DETECTED = "Near ITPB (Whitefield), Bangalore"
    MAP_ADDRESS = "Pattandur Agrahara ECC Rd, Whitefield"

    def _confirm(self, proposal, lat, lng, value=None):
        import json
        from app.agents.adzump.tools.campaign_data import _store_confirmed_location
        session_ctx = {"product_data": {"product_name": "Misty Shores", "place": {}},
                       "_pending_location_confirm": proposal}
        reply = json.dumps({"type": "location_update", "lat": lat, "lng": lng,
                            "address": self.MAP_ADDRESS})
        _store_confirmed_location(session_ctx, value or self.MAP_ADDRESS, reply)
        return session_ctx

    def test_table(self):
        sent = {"address": self.DETECTED, "lat": 12.97, "lng": 77.73}
        rows = [  # (case, proposal, pin lat, pin lng, expected address)
            ("untouched pin keeps detected", sent, 12.97, 77.73, self.DETECTED),
            ("moved pin takes the map address", sent, 12.99, 77.70, self.MAP_ADDRESS),
            ("no sent coords: map address", {"address": self.DETECTED}, 12.97, 77.73,
             self.MAP_ADDRESS),
            ("legacy string proposal: map address", self.DETECTED, 12.97, 77.73,
             self.MAP_ADDRESS),
        ]
        for case, proposal, lat, lng, expected in rows:
            with self.subTest(case):
                ctx = self._confirm(proposal, lat, lng)
                place = ctx["product_data"]["place"]
                self.assertEqual(place["address"], expected)
                self.assertEqual((place["lat"], place["lng"]), (lat, lng))
                self.assertEqual(ctx["campaign_spec"]["location"], expected)
                self.assertNotIn("_pending_location_confirm", ctx)


class WantsCompetitorCreativesTests(unittest.TestCase):
    """The ONE consent predicate behind fetch_competitor_creatives' hard gate
    and the creatives step's said-yes prescription - they must never disagree."""

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
            ("don't fetch their ads", False),               # negation voids the verb
            ("skip fetching their ads for now", False),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(wants_competitor_creatives(text), expected)


class LastUserTextTests(unittest.TestCase):
    """What the HUMAN last typed - role="user" tool_result carriers are skipped."""

    def test_table(self):
        from app.agents.adzump.tools.campaign_data import _last_user_text

        tool_result_msg = {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "found 5"}]}
        assistant_msg = {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "analyze_competitors", "input": {}}]}

        cases = [
            ("plain string", [{"role": "user", "content": "Yes"}], "Yes"),
            ("text blocks", [{"role": "user", "content": [
                {"type": "text", "text": "show me"}, {"type": "text", "text": "their ads"}]}],
             "show me their ads"),
            ("skips tool_result carrier back to the human",
             [{"role": "user", "content": "Yes"}, assistant_msg, tool_result_msg], "Yes"),
            ("skips several tool_result carriers",
             [{"role": "user", "content": "Yes"}, assistant_msg, tool_result_msg,
              assistant_msg, tool_result_msg], "Yes"),
            ("tool results only - no human text yet", [tool_result_msg], ""),
            ("image-only message IS the latest human message",
             [{"role": "user", "content": "Yes"},
              {"role": "user", "content": [{"type": "image", "source": {}}]}], ""),
            ("no messages", [], ""),
        ]
        for name, messages, expected in cases:
            with self.subTest(case=name):
                session = make_session()
                session.messages = messages
                self.assertEqual(_last_user_text({"_session": session}), expected)


class PendingCreativesFetchSteerTests(unittest.TestCase):
    """analyze_competitors results carry the fetch reminder while a consented
    creative fetch is still owed - live 2026-07-29: the model burned the Yes
    on a pre-analysis fetch attempt, then never fetched after analyzing."""

    def test_table(self):
        from app.agents.adzump.tools.campaign_data import (
            CREATIVES_REVIEW_ASK, pending_creatives_fetch_steer,
        )

        def ctx(*, platform="Meta", accepted=False, fetched=False, last_user="Yes",
                messages=None):
            spec = {"platform": platform}
            if accepted:
                spec["competitor_creatives"] = "accepted"
            extra = {}
            if fetched:  # covered = every named competitor carries a result
                extra["competitor_analysis"] = {"competitors": [
                    {"name": "R", "url": "https://r.com", "creatives": []}]}
            session = make_session(last_user=last_user, spec=spec, **extra)
            if messages is not None:
                session.messages = messages
            return {"session_context": session.context, "_session": session}

        # The shape the steer was written for: analyze_competitors just ran,
        # so its tool_result (a role="user" message) sits after the human Yes.
        mid_turn = [
            {"role": "user", "content": "Yes"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "analyze_competitors",
                 "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "found 5"}]},
        ]

        cases = [
            ("owed: fresh yes + unfetched", ctx(), True),
            ("owed: stored ACCEPTED survives a digression",
             ctx(accepted=True, last_user="what about targeting?"), True),
            ("owed mid-turn, after analyze's tool_result", ctx(messages=mid_turn), True),
            ("google flow", ctx(platform="Google Ads"), False),
            ("already fetched (resolved)", ctx(fetched=True), False),
            ("reply is not consent, nothing stored", ctx(last_user="30 days"), False),
        ]
        for name, context, owed in cases:
            with self.subTest(case=name):
                steer = pending_creatives_fetch_steer(context)
                self.assertEqual(bool(steer), owed)
                if owed:  # the review ask the step prescribes, never a fetch-now
                    self.assertIn(CREATIVES_REVIEW_ASK, steer)


class ClearHelperTests(unittest.TestCase):
    """F26: once competitors exist, a prior decline is void - cleared with its
    provenance, under either field name; an accepted offer stands."""

    def test_rows(self):
        google = {"platform": "Google Ads"}
        for label, extra, popped in [
            ("the enum decline", {"competitive_analysis": "declined"}, True),
            ("the legacy flag", {"competitive_analysis_declined": "true"}, True),
            ("an accepted offer stands", {"competitive_analysis": "accepted"}, False),
            ("nothing to clear", {}, False),
        ]:
            with self.subTest(label):
                sc = {"campaign_spec": {**google, **extra},
                      "_spec_set_at": {"platform": 1, **{k: 3 for k in extra}}}
                self.assertEqual(clear_competitor_decline(sc), popped)
                kept = google if popped else {**google, **extra}
                self.assertEqual(sc["campaign_spec"], kept)
                self.assertEqual(set(sc["_spec_set_at"]), set(kept))  # provenance in lockstep
        self.assertFalse(clear_competitor_decline({}))            # missing dicts: no crash


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
