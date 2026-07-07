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

import json
import unittest

from app.core.session import _tool_only_turn_note


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


if __name__ == "__main__":
    unittest.main()
