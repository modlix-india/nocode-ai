"""Unit tests for app/agents/adzump/tools/suggestions.py (_present_options, _norm_q)."""
# regression: F9 (present_options question de-dup) + tagged-capture elicit tagging
from __future__ import annotations

import asyncio
import types
import unittest

from app.agents.adzump.tools.suggestions import _present_options, _norm_q
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
    Q = "How long should the campaign run?"

    def test_skips_emit_when_question_already_streamed(self):
        stream = FakeStream()
        res = _run(self.Q, "Got it.\n\nHow long should the campaign run?", stream)
        self.assertTrue(res.success)
        self.assertEqual(stream.texts, [])                       # no double-render

    def test_emits_when_not_streamed(self):
        stream = FakeStream()
        res = _run(self.Q, "Got it.", stream)                    # only a lead-in
        self.assertTrue(res.success)
        self.assertTrue(any("How long should the campaign run?" in t for t in stream.texts))

    def test_dedup_is_whitespace_and_punctuation_insensitive(self):
        stream = FakeStream()
        # Different spacing + no trailing '?' in the prose - should still match.
        _run(self.Q, "ok... how long   should the campaign run", stream)
        self.assertEqual(stream.texts, [])

    def test_divergent_paraphrase_still_emits(self):
        stream = FakeStream()
        _run(self.Q, "Got it — how many days do you want to run this?", stream)
        self.assertTrue(any(self.Q in t for t in stream.texts))  # documented limit

    def test_no_session_falls_through_to_emit(self):
        # No _session on context → no streamed text → emit (back-compat / safety).
        stream = FakeStream()
        asyncio.run(_present_options(
            {"question": self.Q, "options": ["30 days", "Custom"]},
            {"event_stream": stream, "session_context": {}},
        ))
        self.assertTrue(any(self.Q in t for t in stream.texts))

    def test_norm_q(self):
        self.assertEqual(_norm_q("How long should it run?"), "how long should it run")
        self.assertEqual(_norm_q("  HOW   long  "), "how long")     # lower + collapse + strip


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

    def test_untagged_data_is_none(self):
        res = asyncio.run(_present_options(
            {"question": "Launch?", "options": ["Yes", "No"]},
            {"session_context": {}}))
        self.assertIsNone(res.data)

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


class CaptureAckBackstopTests(unittest.TestCase):
    """S1-7/R7 - a landed capture is always visibly acknowledged, never twice."""

    Q = "What's your daily budget?"

    def _run_with_pending(self, turn_text):
        stream = FakeStream()
        ctx = _ctx(turn_text, stream)
        ctx["session_context"]["_capture_ack_pending"] = {
            "field": "duration", "value": "30 days"}
        asyncio.run(_present_options(
            {"question": self.Q, "options": ["₹5,000/day", "Custom"]}, ctx))
        return stream, ctx["session_context"]

    def test_ack_prepended_when_prose_missed_it(self):
        stream, sc = self._run_with_pending("Sure!")
        joined = "".join(stream.texts)
        self.assertLess(joined.index("30 days"), joined.index(self.Q))  # ack first
        self.assertNotIn("_capture_ack_pending", sc)       # consumed

    def test_no_double_ack_when_prose_named_the_value(self):
        stream, sc = self._run_with_pending("Great - 30 days it is!")
        self.assertNotIn("30 days", "".join(stream.texts))
        self.assertNotIn("_capture_ack_pending", sc)

    def test_emit_skip_never_skips_the_ack(self):
        # F9 skips the already-streamed question emit, never the ack.
        stream, sc = self._run_with_pending("What's your daily budget?")
        joined = "".join(stream.texts)
        self.assertIn("30 days", joined)
        self.assertEqual(joined.count("daily budget"), 0)


if __name__ == "__main__":
    unittest.main()
