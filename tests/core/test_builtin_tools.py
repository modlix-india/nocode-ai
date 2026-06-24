"""Characterization tests for core/builtin_tools (server-tool stream handling).

Currently covers only the crash guard (edge B). The fake stream + chunk here
seed the deferred fuller coverage (happy-path web_search/web_fetch SSE sequence,
batch pattern, empty-query skip, delta-not-cumulative).

    cd nocode-ai && ./venv/bin/python -m unittest tests.core.test_builtin_tools -v
"""

from __future__ import annotations

import unittest

from app.core.builtin_tools import (
    close_builtin_rows, on_builtin_tool_result, on_builtin_tool_use,
)


class _FakeStream:
    """Records emit_* calls; every emit is async."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def emit_tool_start(self, *a, **k):
        self.calls.append(("start", a, k))

    async def emit_tool_update(self, *a, **k):
        self.calls.append(("update", a, k))

    async def emit_tool_result(self, *a, **k):
        self.calls.append(("result", a, k))


class _FakeChunk:
    def __init__(self, tool_id=None, tool_name="web_search", hits=None, text=""):
        self.tool_id = tool_id
        self.tool_name = tool_name
        self.hits = hits or []
        self.text = text


class OrphanedBuiltinResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_orphaned_result_logs_and_skips(self):
        """A builtin_tool_result whose tool_id has no open row must NOT crash.

        Without the guard, ``row.get(...)`` on ``None`` raises AttributeError
        mid-stream and kills the turn. It should log a warning, emit nothing,
        and return.
        """
        rows: dict = {}  # no open rows → the result is orphaned
        stream = _FakeStream()
        chunk = _FakeChunk(
            tool_id="ws_missing", tool_name="web_search",
            hits=[{"title": "x", "url": "http://a.com"}],
        )
        with self.assertLogs("app.core.builtin_tools", level="WARNING") as log:
            await on_builtin_tool_result(rows, chunk, stream)  # must not raise
        self.assertEqual(stream.calls, [])  # nothing emitted for an orphan
        self.assertTrue(any("orphaned" in line for line in log.output))


class WebSearchSSESequenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_use_result_close_delta_not_cumulative(self):
        """Pins the happy-path SSE sequence + the delta-not-cumulative rule:

        - tool_use   → emit_tool_start + emit_tool_update(query)
        - tool_result→ emit_tool_update(ONLY the rendered hits delta, not the query)
        - close      → emit_tool_result(the CUMULATIVE summary: query line + hits)
        """
        rows: dict = {}
        stream = _FakeStream()
        await on_builtin_tool_use(
            rows, _FakeChunk(tool_id="ws1", tool_name="web_search", text="best CRM 2026"), stream,
        )
        await on_builtin_tool_result(
            rows,
            _FakeChunk(
                tool_id="ws1", tool_name="web_search",
                hits=[{"title": "Acme", "url": "https://acme.com/x"}], text="",
            ),
            stream,
        )
        await close_builtin_rows(rows, stream)

        self.assertEqual([c[0] for c in stream.calls], ["start", "update", "update", "result"])

        # the result-phase update is the DELTA (rendered hits only), NOT cumulative
        delta_text = stream.calls[2][1][1]
        self.assertIn("Acme", delta_text)
        self.assertNotIn("web_search · ", delta_text)  # query line is NOT re-sent

        # the close emit carries the CUMULATIVE summary (query line + hits)
        summary = stream.calls[3][1][2]
        self.assertIn("web_search · best CRM 2026", summary)
        self.assertIn("Acme", summary)


if __name__ == "__main__":
    unittest.main()
