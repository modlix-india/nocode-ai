"""LocationAgent.handle - the orchestrator-facing entry point.

The orchestrator routes the user's verbatim message here; handle() runs the
agent's OWN tool-use loop - no separate interpreter agent, no code-side
dispatch - so the model acts by picking one tool. These tests lock that
contract by mocking the loop (LocationAgent.run) and verifying the prompt
content and the post-run result composition.
"""
from __future__ import annotations

import asyncio
import contextlib
import unittest
from unittest import mock

from app.agents.adzump.agents.location import agent as agent_mod
from app.agents.adzump.agents.location.agent import get_location_agent
from app.agents.adzump.agents.location.tools._shared import GEO_FINALIZED_KEY


class FakeSubSession:
    """Just enough of BaseSession for build_run_result: context + messages."""

    def __init__(self):
        self.context = {}
        self.messages = []

    def get_messages(self):
        return self.messages

    def say(self, text):
        self.messages.append(
            {"role": "assistant", "content": [{"type": "text", "text": text}]}
        )


def _ctx(target_areas=None):
    return {
        "auth": object(),
        "session_context": {
            "product_data": {
                "product_name": "Purva Heights",
                "target_areas": list(target_areas or []),
            },
            "campaign_spec": {"platform": "Meta"},
        },
    }


def _run(coro):
    return asyncio.run(coro)


class HandleTests(unittest.TestCase):
    """handle() = guards → enrich → ONE self.run → result from post-run state."""

    @contextlib.contextmanager
    def _patched(self, sub_session, run_mock):
        """Patch the loop + the two helpers with side effects (geocode, DB)."""
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(agent_mod.LocationAgent, "run", run_mock))
            stack.enter_context(mock.patch.object(
                agent_mod, "build_sub_session",
                mock.AsyncMock(return_value=sub_session)))
            stack.enter_context(mock.patch.object(
                agent_mod, "resolve_coordinates",
                mock.AsyncMock(return_value=None)))
            yield

    def test_guards_reject_without_running_the_loop(self):
        """Missing auth / session context fail fast and never wake the LLM.
        (The empty-message guard lives in the TOOL WRAPPER - the entry point
        owns it; see ToolWrapperGuardTests. PR #91 J4 deleted the duplicate.)"""
        def no_session_ctx():
            ctx = _ctx()
            ctx.pop("session_context")
            return ctx

        def no_auth():
            ctx = _ctx()
            ctx.pop("auth")
            return ctx

        guards = [
            ("add Mumbai", no_auth(), "auth"),
            ("add Mumbai", no_session_ctx(), "session context"),
        ]
        for user_message, ctx, error_word in guards:
            with self.subTest(error=error_word), mock.patch.object(
                agent_mod.LocationAgent, "run",
                side_effect=AssertionError("the loop must not run past a guard"),
            ):
                res = _run(get_location_agent().handle(user_message, ctx))
                self.assertFalse(res.success)
                self.assertIn(error_word, res.error.lower())

    def test_run_prompt_carries_verbatim_request_and_current_list(self):
        """The model is the interpreter - it must SEE the raw request and the
        1-based list so 'the second area' can map to delete_location(index=2)."""
        sub = FakeSubSession()

        async def fake_run(user_message, session, event_stream):
            session.context[GEO_FINALIZED_KEY] = True
            sub.say("Removed Juhu - 1 area left.")

        ctx = _ctx([{"name": "Andheri"}, {"name": "Juhu"}])
        run_mock = mock.AsyncMock(side_effect=fake_run)
        with self._patched(sub, run_mock):
            res = _run(get_location_agent().handle("delete the second one", ctx))

        self.assertTrue(res.success)
        prompt = run_mock.await_args.kwargs["user_message"]
        self.assertIn('"""delete the second one"""', prompt)
        self.assertIn("1. Andheri", prompt)
        self.assertIn("2. Juhu", prompt)
        self.assertIn("Purva Heights", prompt)

    def test_result_success_requires_finalize_stamp(self):
        """A chatty run that never reached finalize_targets must not read as
        success - and its own final text is the error the orchestrator sees."""
        sub = FakeSubSession()

        async def chatty_run(user_message, session, event_stream):
            sub.say("There are only 2 areas, so index 5 is invalid.")

        with self._patched(sub, mock.AsyncMock(side_effect=chatty_run)):
            res = _run(get_location_agent().handle("delete the fifth", _ctx()))
        self.assertFalse(res.success)
        self.assertIn("only 2 areas", res.error)

    def test_success_result_is_audience_both_with_the_agents_summary(self):
        sub = FakeSubSession()

        async def good_run(user_message, session, event_stream):
            session.context[GEO_FINALIZED_KEY] = True
            sub.say("Now targeting 4 prime Bengaluru neighborhoods within 5 km.")

        ctx = _ctx()
        ctx["session_context"]["product_data"]["target_areas"] = [{"name": "HSR"}]
        with self._patched(sub, mock.AsyncMock(side_effect=good_run)):
            res = _run(get_location_agent().handle("set targeting", ctx))
        self.assertTrue(res.success)
        self.assertEqual(res.audience, "both")
        self.assertIn("Bengaluru", res.summary)
        self.assertTrue(res.model_summary)

    def test_run_exception_becomes_structured_error_not_a_raise(self):
        sub = FakeSubSession()
        with self._patched(sub, mock.AsyncMock(side_effect=RuntimeError("provider down"))):
            res = _run(get_location_agent().handle("add Mumbai", _ctx()))
        self.assertFalse(res.success)


class ToolWrapperGuardTests(unittest.TestCase):
    """The orchestrator-side tool wrapper is the ONE owner of the
    empty-message guard - its retry-hint error is what the orchestrator
    relays; handle() assumes a non-empty message."""

    def test_empty_user_message_rejected_with_retry_hint(self):
        from app.agents.adzump.tools.location import manage_targeting_locations
        for params in ({}, {"user_message": ""}, {"user_message": "   "}):
            with self.subTest(params=params):
                res = _run(manage_targeting_locations.execute(params, {}))
                self.assertFalse(res.success)
                self.assertIn("verbatim", res.error)


class ToolRegistryTests(unittest.TestCase):
    """The agent's whole action space is its tool set - lock the four names."""

    def test_agent_registers_discovery_and_edit_tools(self):
        self.assertEqual(
            set(get_location_agent().tools),
            {"discover_neighborhoods", "geocode_recommendations",
             "add_location", "delete_location"},
        )


if __name__ == "__main__":
    unittest.main()
