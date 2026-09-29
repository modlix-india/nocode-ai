"""AdzumpAgent orchestration seams: _capture_tagged_answer,
_resume_elicitation_section, _record_prose_decline, the journey engine
(incl. the Instagram-optional branch), get_pending_suggestions, _advance_chip,
AdzumpContext.from_session.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump.test_agent -v
"""
from __future__ import annotations

import asyncio
import json
import types
import unittest
from unittest import mock

from app.agents.adzump.agent import (
    AdzumpAgent, AdzumpContext,
)
from app.agents.adzump.workflow import ESCAPE_AFTER_ASKS, NEW_CAMPAIGN
from tests.agents.adzump._fixtures import (
    RE, SAAS, elicitation, make_actx, make_session,
)


def _cap(s, turn=1):
    return AdzumpAgent._capture_tagged_answer(None, s, turn=turn)


def _suggest(s, text):
    return asyncio.run(AdzumpAgent.get_pending_suggestions(None, s, text))


def _dur_pe():
    return elicitation("duration", {"30 days": "30 days", "60 days": "60 days"})


def _ig_pe():
    return elicitation("instagram", {"accepted": "accepted", "declined": "declined"})


def _budget_pe(**extra):
    return elicitation("budget", {"₹5,000/day": "₹5,000/day",
                                  "₹10,000/day": "₹10,000/day",
                                  "₹25,000/day": "₹25,000/day"}, **extra)


META_DONE = {"platform": "Meta", "duration": "30 days", "budget": "$50/day",
             "parent_account": "P", "account": "A", "fb_page": "F", "ig_page": "I"}


class FromSessionTests(unittest.TestCase):
    META = META_DONE

    def test_from_session_reads_rail_and_ig_data(self):
        # slice 1d: the offered markers are gone - from_session reads the open
        # rail (legacy field name canonicalized) and the fetched-IG data key.
        s = make_session(spec=dict(self.META), ig_accounts=[],
                         pending_elicitation=elicitation(
                             "competitor_creatives_declined"))
        actx = AdzumpContext.from_session(s)
        self.assertEqual(actx.pending_ask_field, "competitor_creatives")
        self.assertTrue(actx.ig_accounts_fetched)
        bare = AdzumpContext.from_session(make_session(spec=dict(self.META)))
        self.assertIsNone(bare.pending_ask_field)
        self.assertFalse(bare.ig_accounts_fetched)


# ── F3 · Instagram is optional ──────────────────────────────────────────────
class InstagramOptionalTests(unittest.TestCase):
    META_FULL = {"platform": "Meta", "duration": "30 days", "budget": "$50/day",
                 "parent_account": "P", "account": "A", "fb_page": "F"}

    def test_skip_cues(self):
        # regression: F3 (Instagram optional)
        from app.agents.adzump.tools.campaign_data import is_ig_skip
        for text, expected in [
            ("skip insta page", True), ("lets do it later", True),
            ("facebook only", True), ("continue with facebook only", True),
            ("no instagram", True), ("skip", True),
            ("link instagram", False), ("yes please", False),
            ("proceed", False), ("pick the first one", False),
        ]:
            with self.subTest(text=text):
                self.assertEqual(bool(is_ig_skip(text)), expected)

    def test_journey_rows(self):
        # regression: F3 (Instagram optional) / v5 (fetch-time is not render-time)
        for label, user, fetched, present, absent in [
            ("first it checks for a linked account", "", False,
             ["fetch_meta_ig_accounts"], []),
            ("a skip cue records the decline", "skip insta page", False,
             ['instagram="declined"'], ["fetch_meta_ig_accounts"]),
            # the model trusted "already on screen" and skipped present_options live
            ("already fetched: render the choice, never refetch", "proceed", True,
             ["present_options"], ["Call `fetch_meta_ig_accounts(page_id=", "ALREADY on screen"]),
        ]:
            with self.subTest(label):
                missing = "\n".join(NEW_CAMPAIGN.walk(make_actx(
                    dict(self.META_FULL), product=SAAS, last_user=user,
                    ig_fetched=fetched)).missing)
                for token in present:
                    self.assertIn(token, missing)
                for token in absent:
                    self.assertNotIn(token, missing)


# ── PR2 / F17b · tagged-answer capture ──────────────────────────────────────
class ProseDeclineTests(unittest.TestCase):
    """A typed "no" is saved as declining competitor analysis only when that
    offer was asked in plain text - never when another question is open, where
    the "no" answers that question (a "No" to the ad-account question was
    saved as an analysis decline)."""

    def test_rows(self):
        google = {"platform": "Google Ads"}
        for label, spec, pe, user, recorded in [
            ("a typed no to the offer asked in plain text", google, None, "no thanks", True),
            ("a no to another open question", google,
             elicitation("account", {"4461972633": "4461972633"}), "No", False),
            ("a no to an open map or other widget", google,
             {"tool": "confirm_location", "expects": "single"}, "no", False),
            ("the offer's own chip question: tagged capture's", google,
             elicitation("competitive_analysis", {"Yes": "accepted", "No": "declined"}),
             "no thanks", False),
            ("an unclear reply stays with the model", google, None,
             "no competitors named yet", False),
            ("a Meta campaign has no analysis offer", {"platform": "Meta"}, None,
             "no thanks", False),
        ]:
            with self.subTest(label):
                s = make_session(last_user=user, spec=dict(spec), pending_elicitation=pe)
                actx = AdzumpContext.from_session(s)
                saved = AdzumpAgent._record_prose_decline(None, s, actx, user, 1)
                self.assertEqual(saved, recorded)
                self.assertEqual(
                    s.context["campaign_spec"].get("competitive_analysis") == "declined",
                    recorded)


class TaggedCaptureTests(unittest.TestCase):
    """Chip answers and tight typed values store with provenance and consume
    the elicitation; ambiguity falls through to the model (F17b: an ambiguous
    "no…" must never auto-record the competitor decline)."""

    def test_table(self):
        decline = elicitation("competitive_analysis_declined")
        creatives_decline = elicitation("competitor_creatives_declined")
        cases = [  # (name, pe, user, stored, consumed)
            ("creatives decline chip", creatives_decline, "No",
             {"competitor_creatives": "declined"}, True),
            ("offer yes chip writes accepted",
             elicitation("competitive_analysis", {"Yes": "accepted", "No": "declined"}),
             "Yes", {"competitive_analysis": "accepted"}, True),
            ("creatives yes chip writes accepted",
             elicitation("competitor_creatives", {"Yes": "accepted", "No": "declined"}),
             "Yes", {"competitor_creatives": "accepted"}, True),
            # live 2026-09-28: a chip sending the bare answer failed the phrase
            # check and the Instagram question was asked twice.
            ("instagram chip sending its answer", _ig_pe(), "declined",
             {"instagram": "declined"}, True),
            ("instagram takes only a decline", _ig_pe(), "accepted", {}, False),
            ("offer chip sending its answer",
             elicitation("competitive_analysis", {"declined": "declined"}),
             "declined", {"competitive_analysis": "declined"}, True),
            ("duration chip", _dur_pe(), "30 days",
             {"duration": "30 days"}, True),
            ("budget preset chip", _budget_pe(), "₹10,000/day",
             {"budget": "₹10,000/day"}, True),
            ("decline chip", decline, "No",
             {"competitive_analysis": "declined"}, True),
            ("typed clear decline", decline, "no thanks, skip it",  # the live F17b message
             {"competitive_analysis": "declined"}, True),
            # Typed values fall through to the steered model (layer 2) - the
            # regex parser is retired (slice 1b); only exact chips + clear
            # declines capture in code.
            ("typed duration falls to layer 2", _dur_pe(), "25 days", {}, False),
            ("typed budget falls to layer 2", elicitation("budget", {}), "4k", {}, False),
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
                    self.assertTrue(ack)  # D14: the model is told it's stored
                    self.assertEqual(s.context.get("_captured_this_turn"), pe["field"])  # F4
                    for f in stored:
                        self.assertEqual(s.context["_spec_set_at"].get(f), 1)  # provenance
                    if name == "account pick by known id":
                        # The model is told what the user clicked (the account's
                        # name, not its id) and words the acknowledgement itself.
                        self.assertIn("**Main Account**", ack)
                        self.assertNotIn("Got it", ack)
                else:
                    self.assertEqual(ack, "")
                    self.assertIsNotNone(s.context.get("_pending_elicitation"))

    def test_platform_chip_names_reused_accounts(self):
        # A click that silently picks the ad account the money goes through
        # must tell the model, so the reply names it (live 2026-09-24).
        product = {**RE, "ad_accounts": {"meta": {
            "parent_account": "B1", "account": "A1",
            "names": {"B1": "AdZump Dummy", "A1": "my campaign"}}}}
        s = make_session(last_user="Meta", product=product,
                         pending_elicitation=elicitation("platform", {"Meta": "Meta"}))
        ack = _cap(s)
        self.assertEqual(s.context["campaign_spec"]["account"], "A1")
        for name in ("AdZump Dummy", "my campaign"):
            self.assertIn(name, ack)
        with self.subTest("a plain capture names no accounts"):
            s = make_session(last_user="30 days", product=product,
                             pending_elicitation=_dur_pe())
            self.assertNotIn("AdZump Dummy", _cap(s))

    def test_turn_gate(self):
        # regression: PR2 (resume gated on agentic-loop turn, not _turn_count)
        s = make_session(last_user="30 days", pending_elicitation=_dur_pe(), turn=5)
        _cap(s, turn=1)                                       # resume restores _turn_count=5
        self.assertEqual(s.context["campaign_spec"].get("duration"), "30 days")
        s = make_session(last_user="30 days", pending_elicitation=_dur_pe())
        self.assertEqual(_cap(s, turn=2), "")                 # later agentic turns are noops
        self.assertEqual(s.context["campaign_spec"], {})
        self.assertIsNotNone(s.context["_pending_elicitation"])


# ── F4 / F23 · the chips under a reply, in get_pending_suggestions order ────
class PendingSuggestionsTests(unittest.TestCase):
    def test_rows(self):
        queued = {"options": [{"label": "x", "value": "x"}], "mode": "single"}
        inferred = {"options": [1], "mode": "single"}
        advance = "Got it. Let's confirm the location for the campaign."
        # (label, session extras, reply text, expected, infer runs)
        for label, extra, text, expected, infers in [
            ("queued chips win", {"_captured_this_turn": "duration",
                                  "_pending_suggestions": queued}, "", queued, False),
            ("a map on screen owns the ask", {"_pending_location_confirm": {"address": "x"}},
             advance, None, False),
            ("a question widget owns the ask",
             {"pending_elicitation": {"tool": "present_options", "field": "duration"}},
             advance, None, False),
            ("a prose advance ask gets one value-only chip, even after a capture",
             {"_captured_this_turn": "platform"}, advance,
             {"options": [{"label": "Confirm location", "value": "yes, confirm the location"}],
              "mode": "single"}, False),
            ("no guessed chips right after a capture", {"_captured_this_turn": "duration"},
             "How long should it run?", None, False),
            ("otherwise inferred from the reply", {}, "How long?", inferred, True),
        ]:
            with self.subTest(label):
                s = make_session(last_user="30 days", **extra)
                infer = mock.AsyncMock(return_value=inferred)
                with mock.patch("app.agents.adzump.agent.infer_suggestions", new=infer):
                    self.assertEqual(_suggest(s, text), expected)
                self.assertEqual(infer.await_count, int(infers))
                self.assertNotIn("_captured_this_turn", s.context)  # never leaks a reply


# ── R12 · refused-required-slot escape (slice 1e) ───────────────────────────
class RefusedSlotEscapeTests(unittest.TestCase):
    def test_escape_after_repeated_asks(self):
        # S1-10: repeated unanswered asks switch to one explicit-click
        # recommendation chip - never a silent default.
        def line(field, asks):
            actx = make_actx({"platform": "Google Ads"}, attempted=True,
                             field_asks={field: asks})
            return next(x for x in NEW_CAMPAIGN.walk(actx).missing if x.startswith(field))
        for field in ("duration", "budget"):
            with self.subTest(field):
                self.assertIn('"answer":', line(field, ESCAPE_AFTER_ASKS))
                self.assertNotIn('"answer":', line(field, ESCAPE_AFTER_ASKS - 1))


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


# ── D9 · every data-ask present_options carries field= ─────────────────────
class PrescriptionAuditTests(unittest.TestCase):
    def test_data_asks_are_tagged(self):
        # regression: D9 (every data-ask carries field=); F20: never call syntax
        for label, spec, fields in [
            ("nothing set", {}, ["platform"]),
            ("google", {"platform": "Google Ads"},
             ["competitive_analysis", "duration", "budget"]),
            ("meta, accounts done", META_DONE, ["competitor_creatives"]),
        ]:
            with self.subTest(label):
                missing = NEW_CAMPAIGN.walk(make_actx(dict(spec))).missing
                joined = "\n".join(missing)
                self.assertNotIn("present_options(", joined)
                for field in fields:
                    self.assertIn(f'field "{field}"', joined)


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


class LoopCompleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_is_saved_after_the_end_of_turn_hooks(self):
        # The loop saves the context before _on_loop_complete; the autosave
        # writes competitor row ids back into it, and the next request reloads
        # the context from the database - so it must be saved again after.
        order: list[str] = []
        session = mock.Mock()
        session.save_context = mock.AsyncMock(side_effect=lambda: order.append("save_context"))
        agent = AdzumpAgent.__new__(AdzumpAgent)
        with mock.patch("app.core.agent.BaseAgent._on_loop_complete", new=mock.AsyncMock()), \
             mock.patch.object(AdzumpAgent, "_autosave_campaign",
                               new=mock.AsyncMock(side_effect=lambda s: order.append("autosave"))), \
             mock.patch.object(AdzumpAgent, "_map_targets_for_new_platform", new=mock.AsyncMock()), \
             mock.patch.object(AdzumpAgent, "_emit_stored_targeting_panel", new=mock.AsyncMock()):
            await agent._on_loop_complete(session, [])
        self.assertEqual(order, ["autosave", "save_context"])

    async def test_a_message_reads_nothing_before_the_model(self):
        # Kailash 2026-09-29: a chat that has its product and competitors keeps
        # its own copy; nothing is re-read from the database per message.
        session = mock.Mock(context={"product_data": {"product_name": "Springs"},
                                     "competitor_analysis": {"competitors": [{"name": "Sobha"}]}},
                            session_id="s1")
        agent = AdzumpAgent.__new__(AdzumpAgent)
        with mock.patch("app.agents.adzump.stores.products.get_product",
                        new=mock.AsyncMock()) as m_product, \
             mock.patch("app.agents.adzump.stores.competitors.list_product_competitors",
                        new=mock.AsyncMock()) as m_list, \
             mock.patch("app.core.agent.BaseAgent.run", new=mock.AsyncMock()) as m_model:
            await agent.run("hi", session, object())
        m_model.assert_awaited_once()
        m_product.assert_not_awaited()
        m_list.assert_not_awaited()


class ReminderRecordTests(unittest.TestCase):
    """The record actually fires from build_turn_reminder with real capture
    flow: a chip click lands as a layer-1 capture, and prior_capture rotates."""

    def _reminder(self, s, turn=1):
        agent = AdzumpAgent.get_instance()
        with self.assertLogs("app.agents.adzump.observability", "INFO") as logs:
            asyncio.run(agent.build_turn_reminder(s, turn))
        return json.loads(logs.output[0].split("turn_decision ", 1)[1])

    def test_capture_flows_into_record_and_prior_rotates(self):
        s = make_session(
            last_user="30 days", product=SAAS,
            spec={"platform": "Google Ads", "competitive_analysis": "declined"},
            pending_elicitation=elicitation(
                "duration", {"30 days": "30 days", "60 days": "60 days"}),
        )
        record = self._reminder(s)
        self.assertEqual(record["captures"], [
            {"layer": 1, "field": "duration", "value": "30 days",
             "verdict": "stored"}])
        self.assertIn("capture_ack", record["steers"])
        # The real wiring computes all three offer verdicts each turn.
        self.assertEqual(record["offers"]["competitive_analysis"], "declined")
        self.assertEqual(record["offers"]["competitor_creatives"], "declined")
        self.assertEqual(record["offers"]["instagram"], "open")
        self.assertFalse(record["repeat_ask"])          # duration landed → budget next
        self.assertEqual(record["prescription"], "budget")
        self.assertEqual(s.context["_prior_capture"],
                         {"field": "duration", "verdict": "stored"})
        # Next user message with no capture: the record carries the prior,
        # then rotates it to None.
        s.messages = [{"role": "user", "content": "what does budget mean?"}]
        record2 = self._reminder(s)
        self.assertEqual(record2["prior_capture"],
                         {"field": "duration", "verdict": "stored"})
        self.assertEqual(record2["captures"], [])
        self.assertIsNone(s.context["_prior_capture"])

    def test_an_account_chip_is_saved_on_the_product_and_the_model_told(self):
        rows = [  # (case, product save outcome, note in the reminder)
            ("saved", True, "Saved on the product too"),
            ("save failed", False, "saving it on the product for future campaigns failed"),
        ]
        for case, saved, note in rows:
            with self.subTest(case):
                s = make_session(
                    last_user="Main ad account", product=SAAS,
                    spec={"platform": "Meta", "parent_account": "B1"},
                    pending_elicitation=elicitation("account", {"Main ad account": "A1"}),
                    account_names={"B1": "AdZump Dummy", "A1": "Main ad account"})
                with mock.patch("app.agents.adzump.services.product_service.save_product_fields",
                                new=mock.AsyncMock(return_value=saved)) as m_save, \
                     mock.patch.object(AdzumpAgent, "build_tool_context",
                                       return_value={"client_code": "GRMEL"}):
                    reminder = asyncio.run(AdzumpAgent.get_instance().build_turn_reminder(s, 1))
                self.assertIn(note, reminder)
                self.assertEqual(list(m_save.await_args.args[2]), ["ad_accounts.meta"])


if __name__ == "__main__":
    unittest.main()
