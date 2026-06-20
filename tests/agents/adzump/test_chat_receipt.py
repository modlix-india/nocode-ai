"""F1 · chat-receipt relay — BaseAgent._chat_receipt_text (below the model).

A tool with relay_summary=True has its summary posted to chat (and persisted by
the run loop) unless the model already wrote it this turn. Pure decision → no
agent instance, no live model. Mirrors the present_options/F9 de-dup guard."""
import unittest

from app.core.agent import BaseAgent
from app.core.tools.base import ToolResult

RECEIPT = "Saved your logo. Skipped image 3 (off-product)."


def _r(**kw):
    base = {"success": True, "summary": RECEIPT, "relay_summary": True}
    base.update(kw)
    return ToolResult(**base)


class ChatReceiptTextTests(unittest.TestCase):
    def test_relays_when_opted_in_and_model_silent(self):
        self.assertEqual(BaseAgent._chat_receipt_text(_r(), "Okay, let me handle those."), RECEIPT)

    def test_skips_when_not_opted_in(self):
        self.assertIsNone(BaseAgent._chat_receipt_text(_r(relay_summary=False), ""))

    def test_skips_on_failure(self):
        self.assertIsNone(BaseAgent._chat_receipt_text(_r(success=False, error="x"), ""))

    def test_skips_empty_summary(self):
        self.assertIsNone(BaseAgent._chat_receipt_text(_r(summary=""), "anything"))

    def test_dedups_when_model_already_wrote_it(self):
        # model lead-in already contains the receipt (case/space-insensitive) → skip
        turn = "Saved your logo.  SKIPPED image 3 (off-product). Anything else?"
        self.assertIsNone(BaseAgent._chat_receipt_text(_r(), turn))

    def test_relays_on_partial_overlap(self):
        # model said only part of it → still relay the full receipt
        self.assertEqual(BaseAgent._chat_receipt_text(_r(), "Saved your logo."), RECEIPT)


class RelaySummaryFlagTests(unittest.TestCase):
    def test_defaults_false(self):
        self.assertFalse(ToolResult(success=True).relay_summary)


if __name__ == "__main__":
    unittest.main()
