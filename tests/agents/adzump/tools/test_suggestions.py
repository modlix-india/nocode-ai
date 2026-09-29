"""Unit tests for app/agents/adzump/tools/suggestions.py (_present_options)."""
# regression: F9 (present_options question de-dup) + tagged-capture elicit tagging
from __future__ import annotations

import asyncio
import types
import unittest

from app.agents.adzump.tools.suggestions import _present_options
from tests.agents.adzump._fixtures import FakeStream


def _ctx(turn_text: str, stream: FakeStream):
    s = types.SimpleNamespace()
    s.context = {}
    s._turn_assistant_text = turn_text
    return {"_session": s, "event_stream": stream, "session_context": s.context}


def _run(question, turn_text, stream, options=None):
    return asyncio.run(_present_options(
        {"question": question, "options": options or ["30 days", "Custom"]},
        _ctx(turn_text, stream),
    ))


class QuestionDedupTests(unittest.TestCase):
    """F9: the question renders once - skipped when the model already wrote it
    this turn, emitted otherwise (a paraphrase must never swallow it)."""

    Q = "How long should the campaign run?"

    def test_rows(self):
        for label, streamed, emitted in [
            ("already streamed", "Got it.\n\nHow long should the campaign run?", False),
            ("streamed with other spacing and no '?'",
             "ok... how long   should the campaign run", False),
            ("only a lead-in streamed", "Got it.", True),
            ("a different wording streamed",
             "Got it - how many days do you want to run this?", True),
            ("no session to read", None, True),
        ]:
            with self.subTest(label):
                stream = FakeStream()
                context = ({"event_stream": stream, "session_context": {}} if streamed is None
                           else _ctx(streamed, stream))
                res = asyncio.run(_present_options(
                    {"question": self.Q, "options": ["30 days", "Custom"]}, context))
                self.assertTrue(res.success)
                self.assertEqual(any(self.Q in t for t in stream.texts), emitted)


class PresentOptionsTagTests(unittest.TestCase):
    def test_tagged_returns_answer_map_on_data(self):
        res = asyncio.run(_present_options(
            {"question": "How long?",
             "options": [{"label": "30 days", "value": "30 days", "answer": "30 days"},
                         {"label": "Custom", "value": "Custom", "answer": None}],
             "field": "duration"},
            {"session_context": {}}))
        self.assertEqual(res.data["elicit_field"], "duration")
        self.assertEqual(res.data["elicit_answers"], {"30 days": "30 days"})  # Custom excluded
        untagged = asyncio.run(_present_options(
            {"question": "Launch?", "options": ["Yes", "No"]}, {"session_context": {}}))
        self.assertIsNone(untagged.data)  # a control-flow ask captures nothing

    def test_field_asks_are_counted(self):
        # Offer counts settle a twice-unanswered offer; duration/budget counts
        # drive the R12 escape; control-flow asks (no field) never count.
        offer = {"question": "Want to see competitor ads?",
                 "options": [{"label": "Yes", "value": "Yes", "answer": "accepted"},
                             {"label": "No", "value": "No", "answer": "declined"}],
                 "field": "competitor_creatives"}
        duration = {"question": "How long?",
                    "options": [{"label": "30 days", "value": "30 days", "answer": "30 days"},
                                {"label": "Custom", "value": "Custom", "answer": None}],
                    "field": "duration"}
        control_flow = {"question": "Ready to launch?", "options": ["Yes, launch", "No"]}
        for name, ask, calls, expected in [
            ("offer ask counts every call", offer, 2, {"competitor_creatives": 2}),
            ("data ask counts", duration, 1, {"duration": 1}),
            ("control-flow ask never counts", control_flow, 1, None),
        ]:
            with self.subTest(name):
                session_ctx: dict = {}
                for _ in range(calls):
                    asyncio.run(_present_options(dict(ask), {"session_context": session_ctx}))
                self.assertEqual(session_ctx.get("_field_asks"), expected)

    def test_instagram_ask_waits_for_the_linked_account_check(self):
        # live 2026-09-28: the model stored the Facebook page and asked "Add
        # Instagram / Facebook only" before checking; the check then found none
        # linked and the user was asked a second time.
        ask = {"question": "Add Instagram?",
               "options": [{"label": "Add Instagram", "value": "Add", "answer": None},
                           {"label": "Facebook only", "value": "declined",
                            "answer": "declined"}],
               "field": "instagram"}
        for name, field, session_ctx, allowed in [
            ("refused before the check", "instagram", {}, False),
            ("old field name refused too", "ig_page_declined", {}, False),
            ("account pick refused too", "ig_page", {}, False),
            ("allowed once none were found", "instagram", {"ig_accounts": []}, True),
            ("allowed once accounts were found", "ig_page", {"ig_accounts": ["1"]}, True),
            ("other fields unaffected", "competitor_creatives", {}, True),
        ]:
            with self.subTest(name):
                res = asyncio.run(_present_options(
                    {**ask, "field": field}, {"session_context": session_ctx}))
                self.assertEqual(res.success, allowed)
                if not allowed:
                    self.assertIn("fetch_meta_ig_accounts", res.error)
                    self.assertNotIn("_pending_suggestions", session_ctx)
                    self.assertNotIn("_field_asks", session_ctx)

    def test_field_tagged_option_must_declare_answer(self):
        # S1-1 · every chip on a field-tagged ask says what it writes; a
        # missing "answer" key is the silent-fall-through bug class. An
        # explicit answer=None is a DECLARED fall-through and passes.
        for options in (
            ["30 days", "Custom"],                         # string options
            [{"label": "30 days", "value": "30 days"},
             {"label": "Custom", "value": "Custom"}],      # no answer keys
        ):
            with self.subTest(options=options):
                res = asyncio.run(_present_options(
                    {"question": "How long?", "options": options,
                     "field": "duration"},
                    {"session_context": {}}))
                self.assertFalse(res.success)
                self.assertIn("30 days", res.error)        # names the option
                # Self-healing: the error hands back the corrected options so
                # the retry is a copy-paste, never a dead-end turn (live bug:
                # a Custom-click follow-up died on this refusal).
                self.assertIn('"answer": "30 days"', res.error)
                self.assertIn('"answer": null', res.error)   # Custom fall-through
                # The user never sees the steering text.
                self.assertNotIn("answer", res.display_error)
        # Control-flow ask (no field): string options stay fine.
        res = asyncio.run(_present_options(
            {"question": "Ready to launch?", "options": ["Yes, launch", "No"]},
            {"session_context": {}}))
        self.assertTrue(res.success)


class NoCodeWrittenAckTests(unittest.TestCase):
    """The tool writes only its question - acknowledging the user's answer is
    the model's, in its own words (live 2026-09-25: a code-written "Got it -
    platform: Meta." stacked on the model's own "Got it - Meta.")."""

    def test_emits_only_the_question(self):
        stream = FakeStream()
        _run("What's your daily budget?", "Sure!", stream,
             options=["₹5,000/day", "Custom"])
        self.assertEqual("".join(stream.texts).strip(), "What's your daily budget?")


if __name__ == "__main__":
    unittest.main()
