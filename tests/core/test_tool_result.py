"""ToolResult.to_tool_result_content — what the MODEL sees, routed by audience.
Pure, below the model. The user-facing relay (emit + persist) is the run loop's
job and lives in test_agent_loop.py; here we lock only the model-facing string.

    cd nocode-ai && ./venv/bin/python -m unittest tests.core.test_tool_result -v
"""
from __future__ import annotations

import unittest

from app.core.tools.base import ToolResult


class ToModelContentTests(unittest.TestCase):

    def test_assistant_default_returns_summary(self):
        r = ToolResult(success=True, summary="Did the thing.")
        self.assertEqual(r.audience, "assistant")          # default
        self.assertEqual(r.to_tool_result_content(), "Did the thing.")

    def test_assistant_no_summary_falls_to_data_json(self):
        r = ToolResult(success=True, data={"k": 1})
        self.assertIn('"k": 1', r.to_tool_result_content())

    def test_both_model_sees_the_summary(self):
        # competitors: the model reasons over the list, so it gets the prose too.
        r = ToolResult(success=True, summary="Found 2: Sobha, Prestige.", audience="both")
        self.assertEqual(r.to_tool_result_content(), "Found 2: Sobha, Prestige.")

    def test_user_model_gets_model_summary_NOT_user_prose(self):
        # the whole point: model is blind to the user copy → it can't double it.
        r = ToolResult(success=True, summary="Saved your logo!",
                       audience="user", model_summary="Stored 1, skipped 0.")
        self.assertEqual(r.to_tool_result_content(), "Stored 1, skipped 0.")

    def test_user_without_model_summary_falls_to_data(self):
        r = ToolResult(success=True, summary="Saved.", audience="user", data={"stored": 1})
        out = r.to_tool_result_content()
        self.assertIn('"stored": 1', out)
        self.assertNotIn("Saved.", out)                    # never the user prose

    def test_user_without_model_summary_or_data_is_OK(self):
        r = ToolResult(success=True, summary="Saved.", audience="user")
        self.assertEqual(r.to_tool_result_content(), "OK")

    def test_failure_returns_error_regardless_of_audience(self):
        r = ToolResult(success=False, error="boom", audience="user", summary="x")
        self.assertEqual(r.to_tool_result_content(), "Error: boom")

    def test_long_text_truncates(self):
        r = ToolResult(success=True, summary="x" * (ToolResult.MAX_RESULT_CHARS + 50))
        out = r.to_tool_result_content()
        self.assertTrue(out.endswith("[truncated — use more specific reads to see details]"))


if __name__ == "__main__":
    unittest.main()
