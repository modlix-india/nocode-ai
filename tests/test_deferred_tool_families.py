"""Deferred tool families — withheld from `tools=`, never actually unreachable.

Advertising all 232 tools costs ~26K tokens on every turn of every conversation,
and whole families (messaging, the security admin tail, image ops) go untouched
in most of them. Those are withheld until a session reaches for one.

The danger this must never become is a silent capability loss: a tool the model
cannot see AND cannot discover is a tool that does not exist. These tests pin the
three properties that keep withholding recoverable — every withheld tool is still
named in the index, `search_tools` still finds it, and fetching its schema makes
it callable for the rest of the session.
"""

from __future__ import annotations

import unittest

from app.agents.appbuilder.context import (
    HOT_TOOLS,
    TOOL_GROUPS_SUMMARY,
    deferred_tool_names,
)
from app.agents.appbuilder.tools.registry import ALL_TOOLS


class _Session:
    def __init__(self, fetched=None):
        self.context = {"fetched_schemas": list(fetched or [])}
        self.auth = None
        self.messages = []


def _agent():
    from app.agents.appbuilder.agent import AppBuilderAgent
    from app.core.context import BaseContext

    return AppBuilderAgent(context_builder=BaseContext(), tools=ALL_TOOLS)


class DeferredFamilyTests(unittest.TestCase):
    def test_something_is_actually_deferred(self) -> None:
        self.assertGreater(len(deferred_tool_names()), 20)

    def test_no_hot_tool_is_ever_deferred(self) -> None:
        """HOT schemas were measured to earn their place; deferring one undoes that."""
        self.assertEqual(deferred_tool_names() & frozenset(HOT_TOOLS), frozenset())

    def test_discovery_tools_are_never_deferred(self) -> None:
        """These are the escape hatch — withholding them would trap the model."""
        for meta in ("search_tools", "get_tool_schema"):
            self.assertNotIn(meta, deferred_tool_names())

    def test_every_deferred_tool_is_still_in_the_index(self) -> None:
        """The index is how the model learns a withheld tool exists at all."""
        missing = [n for n in sorted(deferred_tool_names())
                   if f"`{n}`" not in TOOL_GROUPS_SUMMARY]
        self.assertEqual(missing, [], f"deferred but undiscoverable: {missing}")

    def test_every_deferred_tool_is_still_dispatchable(self) -> None:
        """Withheld from `tools=`, but still registered for execution."""
        registered = {t.name for t in ALL_TOOLS}
        self.assertEqual(deferred_tool_names() - registered, frozenset())

    def test_search_tools_still_finds_a_deferred_tool(self) -> None:
        """search_tools reads the full registry, not the advertised list."""
        import asyncio

        from app.agents.appbuilder.tools.meta_tools import _execute_search_tools

        target = sorted(deferred_tool_names())[0]
        res = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            _execute_search_tools({"query": target, "max_results": 5},
                                  {"tools": list(ALL_TOOLS)})
        )
        self.assertTrue(res.success)
        self.assertIn(target, res.summary)

    def test_the_prompt_explains_the_absent_tools(self) -> None:
        """Otherwise the model concludes a listed tool doesn't exist and works around it."""
        self.assertIn("get_tool_schema", TOOL_GROUPS_SUMMARY)
        self.assertIn("absent from your tool definitions", TOOL_GROUPS_SUMMARY)


class WithheldNamesTests(unittest.TestCase):
    def test_cold_session_withholds_the_deferred_families(self) -> None:
        a = _agent()
        self.assertTrue(a.withheld_tool_names(_Session()))

    def test_fetching_a_schema_stops_withholding_that_tool(self) -> None:
        a = _agent()
        target = sorted(a._deferred_tool_names)[0]
        self.assertIn(target, a.withheld_tool_names(_Session()))
        self.assertNotIn(target, a.withheld_tool_names(_Session(fetched=[target])))

    def test_fetching_one_tool_does_not_release_its_whole_family(self) -> None:
        """Release is per tool — the point is to stay small, not to flip a family on."""
        a = _agent()
        both = sorted(a._deferred_tool_names)[:2]
        withheld = a.withheld_tool_names(_Session(fetched=[both[0]]))
        self.assertNotIn(both[0], withheld)
        self.assertIn(both[1], withheld)

    def test_withheld_is_a_subset_of_registered_tools(self) -> None:
        """A name filtered out for this deployment must not linger in the set."""
        a = _agent()
        self.assertLessEqual(a._deferred_tool_names, {t.name for t in ALL_TOOLS})

    def test_base_agent_defaults_to_withholding_nothing(self) -> None:
        from app.core.agent import BaseAgent
        from app.core.context import BaseContext

        class _Plain(BaseAgent):
            async def build_dynamic_context(self, session):  # pragma: no cover
                return ""

        a = _Plain(name="p", tools=[], context_builder=BaseContext())
        self.assertEqual(a.withheld_tool_names(_Session()), set())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
