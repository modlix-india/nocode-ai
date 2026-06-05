"""v4 reliability fixes — F9 present_options question de-dup.

Live bug #10: when the model writes the question as a prose lead-in AND calls
present_options, the question renders twice (the model's copy + the tool's
emit). The tool OWNS the question, so it must emit when the model stayed silent
but skip when the model already streamed it — exactly one copy either way.

The de-dup reads THIS turn's streamed assistant text off the session
(``session._turn_assistant_text``, set by the core run-loop before tool
dispatch) and skips its own emit on a normalized-contains match.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump.test_v4_fixes -v
"""
from __future__ import annotations

import asyncio
import types
import unittest

from app.agents.adzump.tools.suggestions import _present_options, _norm_q
from app.agents.adzump.agent import (
    AdzumpAgent, CampaignContext, _next_action, _is_custom_reply,
)

RE = {"business_type": "real estate", "product_name": "Skyline Villas"}
SAAS = {"business_type": "saas", "product_name": "Acme"}


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


class F9DedupTests(unittest.TestCase):
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
        # Different spacing + no trailing '?' in the prose — should still match.
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


# ── F10 · "Custom" → free-text (stop the chip re-loop) ─────────────────────
def _session(pe, user, *, spec=None, product=RE):
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


def _cctx(spec, *, awaiting=None, product=None, last_user=""):
    return CampaignContext(
        product=product or dict(SAAS), product_profile={}, competitor_names=[],
        competitor_analysis_attempted=False, spec=spec, account_names={}, set_at={},
        current_turn=1, last_user=last_user, pending_location=None, ig_offered=False,
        awaiting_custom_field=awaiting,
    )


class F10CustomTests(unittest.TestCase):
    def test_is_custom_reply(self):
        for s in ("Custom", "custom", "custom amount", "Custom budget"):
            self.assertTrue(_is_custom_reply(s), s)
        for s in ("₹5,000/day", "30 days", "Meta", ""):
            self.assertFalse(_is_custom_reply(s), s)

    def test_custom_click_keeps_elicitation_open_and_steers(self):
        s = _session(_budget_pe(), "Custom")
        ack = AdzumpAgent._capture_tagged_answer(None, s, turn=1)
        self.assertIn("custom value", ack.lower())                 # free-text steer
        self.assertTrue(s.context["_pending_elicitation"].get("awaiting_custom"))  # kept open + marked
        self.assertNotIn("budget", s.context["campaign_spec"])     # NOT stored ("Custom" isn't a value)

    def test_typed_value_after_custom_is_captured(self):
        s = _session(_budget_pe(awaiting_custom=True), "₹7000")
        AdzumpAgent._capture_tagged_answer(None, s, turn=1)
        self.assertEqual(s.context["campaign_spec"].get("budget"), "₹7,000/day")
        self.assertNotIn("_pending_elicitation", s.context)        # consumed on capture

    def test_preset_click_still_captures(self):
        s = _session(_budget_pe(), "₹10,000/day")
        AdzumpAgent._capture_tagged_answer(None, s, turn=1)
        self.assertEqual(s.context["campaign_spec"].get("budget"), "₹10,000/day")
        self.assertNotIn("_pending_elicitation", s.context)

    def test_offtopic_is_not_mistaken_for_custom(self):
        s = _session(_budget_pe(), "what does daily budget mean?")
        ack = AdzumpAgent._capture_tagged_answer(None, s, turn=1)
        self.assertEqual(ack, "")
        self.assertFalse(s.context["_pending_elicitation"].get("awaiting_custom"))  # not marked

    def test_resume_keeps_open_when_awaiting_custom(self):
        s = _session(_budget_pe(awaiting_custom=True), "ok")
        out = AdzumpAgent._resume_elicitation_section(None, s, turn=1)
        self.assertEqual(out, "")
        self.assertIsNotNone(s.context.get("_pending_elicitation"))  # NOT popped
        self.assertTrue(s.context["_pending_elicitation"].get("awaiting_custom"))

    def test_next_action_prescribes_free_text_when_awaiting(self):
        m = _next_action(_cctx({"platform": "Meta", "duration": "30 days",
                                "parent_account": "P", "account": "A"}, awaiting="budget"))
        budget = [x for x in m if x.startswith("budget")]
        self.assertTrue(budget, m)
        self.assertIn("TYPE", budget[0])
        # The free-text prescription may *name* present_options in a "do NOT call"
        # instruction — discriminate on the chip-CALL signature, which must be absent.
        self.assertNotIn("present_options(question", budget[0])     # no chip ask

    def test_next_action_shows_chips_when_not_awaiting(self):
        m = _next_action(_cctx({"platform": "Meta", "duration": "30 days",
                                "parent_account": "P", "account": "A"}))
        budget = [x for x in m if x.startswith("budget")]
        self.assertTrue(budget, m)
        self.assertIn("present_options", budget[0])                # normal chip ask

    def test_from_session_resolves_awaiting_custom(self):
        # Regression: from_session must NOT raise (the walrus-in-conditional
        # UnboundLocalError) and must resolve awaiting_custom_field. The other
        # F10 tests build CampaignContext directly, bypassing from_session —
        # this exercises the live path.
        self.assertEqual(
            CampaignContext.from_session(_session(_budget_pe(awaiting_custom=True), "₹7000")).awaiting_custom_field,
            "budget")
        self.assertIsNone(  # elicitation present but not awaiting
            CampaignContext.from_session(_session(_budget_pe(), "₹7000")).awaiting_custom_field)
        self.assertIsNone(  # no elicitation at all
            CampaignContext.from_session(_session(None, "hi")).awaiting_custom_field)


if __name__ == "__main__":
    unittest.main()
