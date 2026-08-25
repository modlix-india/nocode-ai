"""Unit tests for app/agents/adzump/tools/suggestions.py (_present_options, _norm_q)."""
# regression: F9 (present_options question de-dup) + tagged-capture elicit tagging
from __future__ import annotations

import asyncio
import types
import unittest

from app.agents.adzump.tools.suggestions import _present_options, _norm_q


class _FakeStream:
    """Captures emit_text calls; present_options' happy path only uses emit_text."""
    def __init__(self):
        self.texts: list[str] = []

    async def emit_text(self, text: str) -> None:
        self.texts.append(text)


def _ctx(turn_text: str, stream: _FakeStream):
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
        stream = _FakeStream()
        res = _run(self.Q, "Got it.\n\nHow long should the campaign run?", stream)
        self.assertTrue(res.success)
        self.assertEqual(stream.texts, [])                       # no double-render

    def test_emits_when_not_streamed(self):
        stream = _FakeStream()
        res = _run(self.Q, "Got it.", stream)                    # only a lead-in
        self.assertTrue(res.success)
        self.assertTrue(any("How long should the campaign run?" in t for t in stream.texts))

    def test_dedup_is_whitespace_and_punctuation_insensitive(self):
        stream = _FakeStream()
        # Different spacing + no trailing '?' in the prose - should still match.
        _run(self.Q, "ok... how long   should the campaign run", stream)
        self.assertEqual(stream.texts, [])

    def test_divergent_paraphrase_still_emits(self):
        stream = _FakeStream()
        _run(self.Q, "Got it — how many days do you want to run this?", stream)
        self.assertTrue(any(self.Q in t for t in stream.texts))  # documented limit

    def test_no_session_falls_through_to_emit(self):
        # No _session on context → no streamed text → emit (back-compat / safety).
        stream = _FakeStream()
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
             "options": [{"label": "30 days", "value": "30 days", "answer": "30 days"}, "Custom"],
             "field": "duration"},
            {"session_context": {}}))
        self.assertEqual(res.data["elicit_field"], "duration")
        self.assertEqual(res.data["elicit_answers"], {"30 days": "30 days"})  # Custom excluded

    def test_untagged_data_is_none(self):
        res = asyncio.run(_present_options(
            {"question": "Launch?", "options": ["Yes", "No"]},
            {"session_context": {}}))
        self.assertIsNone(res.data)

    def test_summary_is_the_bare_question_not_a_meta_frame(self):
        # regression: the summary doubles as the tool-only-turn restore stand-in. A
        # fixed meta-frame ('Presented quick-reply options for: "…"') repeats verbatim
        # across every options turn and the resumed model imitates it into chat, so the
        # summary must be the plain question the user saw.
        res = asyncio.run(_present_options(
            {"question": "Which platform should we run this on?",
             "options": ["Google Ads", "Meta"]},
            {"session_context": {}}))
        self.assertEqual(res.summary, "Which platform should we run this on?")
        self.assertNotIn("Presented quick-reply options", res.summary)


if __name__ == "__main__":
    unittest.main()
