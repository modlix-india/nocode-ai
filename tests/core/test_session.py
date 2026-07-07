"""BaseSession helpers - session history restore behavior.

Regression: a restored turn with an empty assistant_summary used to inject the
placeholder "(Performed actions via tools)" into the LLM message history; a
resumed orchestrator once parroted it verbatim to the user. The stand-in must
read as a transcript note and name the tools that ran.
"""
from __future__ import annotations

import json
import unittest

from app.core.session import _tool_only_turn_note


class ToolOnlyTurnNoteTests(unittest.TestCase):
    def test_note_variants(self):
        variants = [
            ("names tools, deduped",
             json.dumps([{"tool": "present_options"}, {"tool": "present_options"},
                         {"tool": "set_campaign_spec"}]),
             "[transcript note: this turn had no text reply; "
             "tools called: present_options, set_campaign_spec]"),
            ("no tool log",
             None,
             "[transcript note: this turn had no text reply]"),
            ("malformed json survives",
             "{not json",
             "[transcript note: this turn had no text reply]"),
            ("entries without tool key skipped",
             json.dumps([{"input": {}}, "junk"]),
             "[transcript note: this turn had no text reply]"),
        ]
        for label, tool_calls_json, expected in variants:
            with self.subTest(label):
                self.assertEqual(_tool_only_turn_note(tool_calls_json), expected)

    def test_never_the_parrotable_placeholder(self):
        self.assertNotIn("Performed actions", _tool_only_turn_note(None))


if __name__ == "__main__":
    unittest.main()
