"""Unit: core BaseAgent run-loop — _with_tail_reminder (replace-not-append, tail-placed)."""
from __future__ import annotations

import unittest

from app.core.agent import BaseAgent

REMINDER = "State: platform=Google Ads. Next: ask duration."


class WithTailReminderTests(unittest.TestCase):
    def test_empty_reminder_is_passthrough(self):
        msgs = [{"role": "user", "content": "hi"}]
        self.assertIs(BaseAgent._with_tail_reminder(msgs, ""), msgs)

    def test_does_not_mutate_input(self):
        original = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        orig_content = original[-1]["content"]
        orig_blocks = list(orig_content)
        out = BaseAgent._with_tail_reminder(original, REMINDER)
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
        out = BaseAgent._with_tail_reminder([{"role": "user", "content": "what's up"}], REMINDER)
        content = out[-1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "what's up"})
        self.assertIn(REMINDER, content[-1]["text"])

    def test_tool_result_then_reminder(self):
        tr = {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
        out = BaseAgent._with_tail_reminder([{"role": "user", "content": [tr]}], REMINDER)
        content = out[-1]["content"]
        self.assertEqual(content[0], tr)               # tool_result stays first
        self.assertEqual(content[-1]["type"], "text")  # reminder lands last (tail)
        self.assertIn(REMINDER, content[-1]["text"])

    def test_fresh_text_passes_through(self):
        a = BaseAgent._with_tail_reminder([{"role": "user", "content": "x"}], "next: A")
        b = BaseAgent._with_tail_reminder([{"role": "user", "content": "x"}], "next: B")
        self.assertNotEqual(a[-1]["content"][-1]["text"], b[-1]["content"][-1]["text"])

    def test_empty_messages(self):
        out = BaseAgent._with_tail_reminder([], REMINDER)
        self.assertEqual(out[0]["role"], "user")
        self.assertIn(REMINDER, out[0]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
