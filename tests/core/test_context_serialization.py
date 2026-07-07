"""Session context serialization - `_serialize_context` (below the model).

Regression for the bug found in live testing 2026-06-22: `_started_tuids` is a
*set* (opened sub-agent card ids, written by core/streaming.pre_emit_agent_started).
It lived in session.context, so save_context's json.dumps raised "Object of type
set is not JSON serializable" - sinking the ENTIRE context. On AdPilot's
reload-per-message model that wiped the conversation's memory (incl. the open
_pending_elicitation), so every fresh-scrape + upload looped forever.

Fix: strip ephemeral runtime keys before persisting + a set-safe `default=` so a
stray set can never again take the whole context down with it.

    cd nocode-ai && ./venv/bin/python -m unittest tests.core.test_context_serialization -v
"""
from __future__ import annotations

import json
import unittest

from app.core.session import BaseSession

_serialize_context = BaseSession._serialize_context
_EPHEMERAL_CONTEXT_KEYS = BaseSession._EPHEMERAL_CONTEXT_KEYS


class SerializeContextTests(unittest.TestCase):
    def test_the_exact_bug_no_longer_raises(self):
        # The reproduction: a set in the context used to blow up json.dumps.
        ctx = {"product_data": {"name": "X"}, "_started_tuids": {"05edf70ea183", "abc123"}}
        out = _serialize_context(ctx)                 # must not raise
        json.loads(out)                               # must be valid JSON

    def test_ephemeral_keys_are_not_persisted(self):
        ctx = {"product_data": {"name": "X"}, "_started_tuids": {"a", "b"}}
        loaded = json.loads(_serialize_context(ctx))
        self.assertNotIn("_started_tuids", loaded)
        self.assertEqual(loaded["product_data"], {"name": "X"})

    def test_real_state_survives(self):
        # The keys the asset flow depends on must round-trip untouched.
        ctx = {
            "product_data": {"name": "X"},
            "_pending_elicitation": {"tool": "analyze_product",
                                     "payload": {"logo_missing": True,
                                                 "missing_categories": ["hero"]}},
            "_pending_suggestions": {"options": [], "mode": "single"},
            "_started_tuids": {"x"},
        }
        loaded = json.loads(_serialize_context(ctx))
        self.assertEqual(loaded["_pending_elicitation"]["payload"]["logo_missing"], True)
        self.assertEqual(loaded["_pending_elicitation"]["payload"]["missing_categories"], ["hero"])
        self.assertIn("_pending_suggestions", loaded)

    def test_stray_set_degrades_to_list_not_catastrophe(self):
        # Defensive net: a future stray set anywhere becomes a list - the whole
        # context still saves rather than being lost.
        ctx = {"product_data": {"name": "X"}, "some_future_set": {"a", "b"}}
        loaded = json.loads(_serialize_context(ctx))
        self.assertEqual(sorted(loaded["some_future_set"]), ["a", "b"])
        self.assertEqual(loaded["product_data"], {"name": "X"})

    def test_started_tuids_is_a_declared_ephemeral_key(self):
        self.assertIn("_started_tuids", _EPHEMERAL_CONTEXT_KEYS)


if __name__ == "__main__":
    unittest.main()
