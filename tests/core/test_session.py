"""BaseSession helpers - session history restore behavior.

Regression (two generations of the same parrot bug): a restored turn with an
empty assistant_summary used to inject a meta-placeholder into the LLM message
history - first "(Performed actions via tools)", then "[transcript note: this
turn had no text reply; ...]" - and the resumed orchestrator parroted BOTH
verbatim into chat. Any meta-text in the assistant slot gets imitated, so the
stand-in must be built from the tools' own result summaries: text that is
also acceptable user-facing prose.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

from app.core.session import (
    _tool_only_turn_note,
    current_session,
    record_oneshot_usage,
)


class ToolOnlyTurnNoteTests(unittest.TestCase):
    def test_note_variants(self):
        variants = [
            ("tool summaries become the stand-in",
             json.dumps([
                 {"tool": "confirm_location",
                  "summary": "Map + prompt shown for 'Purva, Bengaluru'."},
                 {"tool": "present_options",
                  "summary": "Asked: which platform?"},
             ]),
             "Map + prompt shown for 'Purva, Bengaluru'. Asked: which platform?"),
            ("summaryless tools fall back to a plain receipt",
             json.dumps([{"tool": "present_options"}, {"tool": "present_options"},
                         {"tool": "set_campaign_spec"}]),
             "Done (present_options, set_campaign_spec)."),
            ("no tool log", None, "Done."),
            ("malformed json survives", "{not json", "Done."),
            ("entries without tool key skipped",
             json.dumps([{"input": {}}, "junk"]), "Done."),
        ]
        for label, tool_calls_json, expected in variants:
            with self.subTest(label):
                self.assertEqual(_tool_only_turn_note(tool_calls_json), expected)

    def test_long_summaries_capped(self):
        note = _tool_only_turn_note(
            json.dumps([{"tool": "t", "summary": "x" * 900}]))
        self.assertLessEqual(len(note), 500)

    def test_never_a_parrotable_meta_placeholder(self):
        for tool_calls_json in (None, json.dumps([{"tool": "present_options"}])):
            note = _tool_only_turn_note(tool_calls_json)
            self.assertNotIn("Performed actions", note)
            self.assertNotIn("transcript note", note)
            self.assertNotIn("no text reply", note)


class _CaptureSession:
    """Minimal stand-in for BaseSession: captures the record_token_usage call."""
    agent_name = "campaign"
    auth = None  # real BaseSession always exposes .auth (may be None); the charge is gated on it
    session_id = "test-session"

    def __init__(self):
        self.accumulated: dict | None = None
        self.recorded: dict | None = None

    def accumulate_usage(self, usage: dict) -> None:
        self.accumulated = usage

    async def record_token_usage(self, usage, request_id, model,
                                 provider_name=None, agent_type=None) -> None:
        self.recorded = {"agent_type": agent_type, "model": model,
                         "provider": provider_name, "usage": usage,
                         "request_id": request_id}


class RecordOneshotUsageTests(unittest.TestCase):
    """A one-shot LLM call bills its active session but can carry a distinct
    per-step label so its cost is a separate line in per-agent breakdowns."""

    def _bill(self, sess, **kw):
        token = current_session.set(sess)
        try:
            asyncio.run(record_oneshot_usage(
                {"input_tokens": 10, "output_tokens": 20}, "gpt-4o", **kw))
        finally:
            current_session.reset(token)

    def test_step_labels_the_row(self):
        sess = _CaptureSession()
        self._bill(sess, step="offering_taxonomy")
        # relabelled row...
        self.assertEqual(sess.recorded["agent_type"], "campaign:offering_taxonomy")
        # ...but the live in-memory total still counts it
        self.assertEqual(sess.accumulated, {"input_tokens": 10, "output_tokens": 20})

    def test_no_step_defaults_to_session_agent(self):
        sess = _CaptureSession()
        self._bill(sess)  # no step
        # None -> record_token_usage falls back to the session's agent_name
        self.assertIsNone(sess.recorded["agent_type"])

    def test_no_active_session_is_noop(self):
        # current_session default is None -> no row recorded, no exception
        asyncio.run(record_oneshot_usage({"input_tokens": 1, "output_tokens": 1},
                                         "gpt-4o", step="x"))

    def test_it_charges_the_call_under_the_same_request_id(self):
        # A one-shot OUTSIDE the loop must be CHARGED (billing.charge_llm_call), not just
        # tracked — under the SAME request_id the record used, so the charge is idempotent.
        sess = _CaptureSession()
        sess.auth = mock.MagicMock()
        with mock.patch("app.services.billing.charge_llm_call",
                        new=mock.AsyncMock()) as charge:
            self._bill(sess, step="offering_taxonomy")
        self.assertTrue(charge.called)
        _auth, _usage, _model, req_id, sid = charge.call_args.args
        self.assertEqual(req_id, sess.recorded["request_id"])  # same id → idempotent
        self.assertEqual(sid, sess.session_id)

    def test_no_auth_means_no_charge(self):
        # auth is None (no wallet identity) -> skip the charge, but still track the usage.
        sess = _CaptureSession()  # auth = None
        with mock.patch("app.services.billing.charge_llm_call",
                        new=mock.AsyncMock()) as charge:
            self._bill(sess, step="x")
        self.assertFalse(charge.called)
        self.assertIsNotNone(sess.recorded)  # tracking still happened


if __name__ == "__main__":
    unittest.main()
