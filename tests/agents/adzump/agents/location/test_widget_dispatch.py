"""agents/location/craft.py — handle_widget_message: the router's widget entry point.

Locks the fast-path invariants: the geo layer owns parsing, elicitation
housekeeping, dispatch, and SSE; natural language returns None (→ normal agent
loop); the widget path routes to LocationAgent.add/.delete directly and NEVER
wakes the LLM.
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from app.core.tools.base import ToolResult
from app.agents.adzump.agents.location import craft as craft_mod
from app.agents.adzump.agents.location.craft import handle_widget_message


def _agent_session():
    session = SimpleNamespace(
        context={"_pending_elicitation": {"field": "budget"}},
        session_id="s1",
    )
    agent = SimpleNamespace(
        build_tool_context=lambda s: {"session_context": s.context}
    )
    return agent, session


def _location_agent_stub():
    return SimpleNamespace(
        add=mock.AsyncMock(return_value=ToolResult(success=True, summary="added")),
        delete=mock.AsyncMock(return_value=ToolResult(success=True, summary="deleted")),
    )


async def _drain(response):
    """Consume the SSE body to completion (events() ends on the done sentinel)."""
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return "".join(chunks)


class WidgetDispatchTests(unittest.TestCase):
    def test_natural_language_returns_none(self):
        agent, session = _agent_session()
        self.assertIsNone(handle_widget_message(agent, session, "target the suburbs please"))

    def test_widget_add_dispatches_without_llm(self):
        agent, session = _agent_session()
        stub = _location_agent_stub()

        async def scenario():
            with mock.patch.object(craft_mod, "get_location_agent", return_value=stub), \
                 mock.patch("app.services.llm_provider.get_llm_provider",
                            side_effect=AssertionError("LLM must not be called on the widget fast path")):
                resp = handle_widget_message(
                    agent, session,
                    'add targeting location {"name":"Bandra","lat":19.05,"lng":72.83}',
                )
                self.assertIsNotNone(resp)
                return await asyncio.wait_for(_drain(resp), timeout=5)

        body = asyncio.run(scenario())

        stub.add.assert_awaited_once()                    # routed to LocationAgent.add
        stub.delete.assert_not_awaited()
        params = stub.add.await_args.args[0]
        self.assertEqual(params["action"], "add")
        self.assertEqual(params["name"], "Bandra")
        self.assertNotIn("_pending_elicitation", session.context)   # housekeeping owned here
        self.assertIn("tool_start", body)
        self.assertIn("done", body)

    def test_widget_delete_dispatches(self):
        agent, session = _agent_session()
        stub = _location_agent_stub()

        async def scenario():
            with mock.patch.object(craft_mod, "get_location_agent", return_value=stub):
                resp = handle_widget_message(agent, session, "delete targeting location index 2")
                await asyncio.wait_for(_drain(resp), timeout=5)

        asyncio.run(scenario())
        stub.delete.assert_awaited_once()                 # routed to LocationAgent.delete
        stub.add.assert_not_awaited()
        params = stub.delete.await_args.args[0]
        self.assertEqual(params, {"action": "delete", "index": 2})


if __name__ == "__main__":
    unittest.main()
