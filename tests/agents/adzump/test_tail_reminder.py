"""Approach B (fix 1.1) — tail-reminder invariants.

Guards the two correctness properties that have no other coverage:
  1. _with_tail_reminder is replace-not-append / no-persist — it never mutates
     the input messages (so reminders can't accumulate in session.messages),
     and the reminder lands as the LAST block (tail) wrapped in <system-reminder>.
  2. _resume_elicitation_section is gated to agentic turn==1 — it pops the
     one-shot flag only on turn 1, and on turn>1 returns "" WITHOUT popping
     (so the flag survives Approach B's per-turn rebuild).

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump.test_tail_reminder -v
"""
from __future__ import annotations

import types
import unittest

from app.core.agent import _with_tail_reminder
from app.agents.adzump.agent import AdzumpAgent

REMINDER = "State: platform=Google Ads. Next: ask duration."


class WithTailReminderTests(unittest.TestCase):
    def test_empty_reminder_is_passthrough(self):
        msgs = [{"role": "user", "content": "hi"}]
        self.assertIs(_with_tail_reminder(msgs, ""), msgs)

    def test_does_not_mutate_input(self):
        original = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        orig_content = original[-1]["content"]
        orig_blocks = list(orig_content)
        out = _with_tail_reminder(original, REMINDER)
        # input untouched — the no-persist / replace-not-append guarantee
        self.assertEqual(len(original), 1)
        self.assertIs(original[-1]["content"], orig_content)
        self.assertEqual(original[-1]["content"], orig_blocks)
        # output is a NEW list with the reminder appended at the tail
        self.assertIsNot(out, original)
        last = out[-1]["content"]
        self.assertEqual(len(last), 2)
        self.assertEqual(last[-1]["type"], "text")
        self.assertIn("<system-reminder>", last[-1]["text"])
        self.assertIn(REMINDER, last[-1]["text"])

    def test_string_content_becomes_list_reminder_last(self):
        out = _with_tail_reminder([{"role": "user", "content": "what's up"}], REMINDER)
        content = out[-1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "what's up"})
        self.assertIn(REMINDER, content[-1]["text"])

    def test_tool_result_then_reminder(self):
        tr = {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
        out = _with_tail_reminder([{"role": "user", "content": [tr]}], REMINDER)
        content = out[-1]["content"]
        self.assertEqual(content[0], tr)               # tool_result stays first
        self.assertEqual(content[-1]["type"], "text")  # reminder lands last (tail)
        self.assertIn(REMINDER, content[-1]["text"])

    def test_fresh_text_passes_through(self):
        a = _with_tail_reminder([{"role": "user", "content": "x"}], "next: A")
        b = _with_tail_reminder([{"role": "user", "content": "x"}], "next: B")
        self.assertNotEqual(a[-1]["content"][-1]["text"], b[-1]["content"][-1]["text"])

    def test_empty_messages(self):
        out = _with_tail_reminder([], REMINDER)
        self.assertEqual(out[0]["role"], "user")
        self.assertIn(REMINDER, out[0]["content"][0]["text"])


class ResumeGateTests(unittest.TestCase):
    @staticmethod
    def _session(pe):
        return types.SimpleNamespace(context=({"_pending_elicitation": pe} if pe else {}))

    def test_turn1_single_renders_and_pops(self):
        s = self._session({"expects": "single", "tool": "confirm_location"})
        out = AdzumpAgent._resume_elicitation_section(None, s, turn=1)
        self.assertTrue(out)                                  # rendered
        self.assertNotIn("_pending_elicitation", s.context)   # one-shot popped

    def test_turn2_empty_and_does_not_pop(self):
        s = self._session({"expects": "single", "tool": "confirm_location"})
        out = AdzumpAgent._resume_elicitation_section(None, s, turn=2)
        self.assertEqual(out, "")                             # not rendered on later turns
        self.assertIn("_pending_elicitation", s.context)      # flag survives (NOT popped)

    def test_turn1_no_pending(self):
        s = self._session(None)
        self.assertEqual(AdzumpAgent._resume_elicitation_section(None, s, turn=1), "")


if __name__ == "__main__":
    unittest.main()
