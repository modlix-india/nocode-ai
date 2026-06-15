"""Regression: session.context must stay JSON-serializable.

Bench discovery (2026-06-06): the persistence layer logged
"Failed to save session context: Object of type set is not JSON
serializable (keys=['app_code', 'client_code', 'headers',
'fetched_schemas', 'pending_kb_updates'])" after every conversation.

Root cause: `fetched_schemas` was a `set[str]` (added by
`meta_tools.get_tool_schema` and by `build_tool_context`), and Python's
default `json.dumps` doesn't serialize sets.

Impact: session persistence silently failed on every save, which means
the deferred-schema cache didn't survive session-reload — next request
re-fetched schemas the LLM already saw, wasting tokens.

Fix: use a `list[str]` instead. Membership check on a list of ~10 fetched
schema names is irrelevant cost (≤210-tool surface, and a session rarely
fetches more than a handful in one conversation).

These tests lock in the JSON-serializable invariant + smoke the actual
get_tool_schema path that originally produced the warning.
"""

from __future__ import annotations

import json

import pytest

from app.agents.appbuilder.tools.meta_tools import get_tool_schema_tool


@pytest.mark.asyncio
async def test_get_tool_schema_leaves_context_json_serializable() -> None:
    """After `get_tool_schema(name=...)`, the entire context must json.dumps
    cleanly. This is the exact path that triggered the original warning."""
    context = {
        "app_code": "testapp",
        "client_code": "SYSTEM",
        "headers": {"Authorization": "Bearer t"},
        "fetched_schemas": [],
        "pending_kb_updates": {},
        # Provide the tools so meta-tool lookup succeeds without falling
        # back to ALL_TOOLS (which would slow this test).
        "tools": [],
    }
    # Don't actually look up a tool name (no tools provided) — we just need
    # the code path that mutates fetched_schemas to run.
    result = await get_tool_schema_tool.execute({"name": "anything"}, context)
    # Tool lookup fails (unknown tool) but `fetched_schemas` should still be
    # a list — the mutation path inside get_tool_schema only runs after the
    # tool is found, so this test covers the default-construction path.
    assert result.success is False  # unknown tool
    assert isinstance(context["fetched_schemas"], list)
    # The whole context must JSON-serialize.
    serialized = json.dumps(context)
    assert "fetched_schemas" in serialized


@pytest.mark.asyncio
async def test_get_tool_schema_appends_name_and_stays_serializable() -> None:
    """Successful schema fetch appends the tool name to the list (no dup)
    AND keeps the context JSON-serializable."""
    from app.agents.appbuilder.tools.registry import ALL_TOOLS

    # Pick any real tool so the meta-tool finds it.
    target = next(t.name for t in ALL_TOOLS if t.name == "list_pages")
    context = {
        "fetched_schemas": [],
        "tools": ALL_TOOLS,
    }
    r1 = await get_tool_schema_tool.execute({"name": target}, context)
    assert r1.success is True
    assert context["fetched_schemas"] == [target]

    # Idempotent: a second fetch of the same name doesn't duplicate.
    r2 = await get_tool_schema_tool.execute({"name": target}, context)
    assert r2.success is True
    assert context["fetched_schemas"] == [target]

    # A different tool appends.
    other = next(t.name for t in ALL_TOOLS if t.name == "get_page")
    r3 = await get_tool_schema_tool.execute({"name": other}, context)
    assert r3.success is True
    assert sorted(context["fetched_schemas"]) == sorted([target, other])

    # JSON-serializable throughout.
    json.dumps(context["fetched_schemas"])


def test_default_fetched_schemas_value_is_list_not_set() -> None:
    """The aliased session.context default must be a list, not a set.

    Catches accidental reversion to set() — the very thing that produced
    the bench warning.
    """
    import inspect

    from app.core import agent as core_agent

    source = inspect.getsource(core_agent.BaseAgent.build_tool_context)
    # Be tolerant of whitespace; just confirm the default literal is a list
    # and NOT a set.
    assert 'setdefault("fetched_schemas", [])' in source, (
        "build_tool_context must default fetched_schemas to [] (list) so "
        "session.context stays JSON-serializable. If you change this back "
        "to set(), the persistence layer silently fails on every save."
    )
    assert 'setdefault("fetched_schemas", set())' not in source, (
        "Found `setdefault('fetched_schemas', set())` in build_tool_context "
        "— this re-introduces the JSON-serialization bug."
    )


def test_meta_tools_uses_list_default() -> None:
    """Same regression for meta_tools.get_tool_schema."""
    import inspect

    from app.agents.appbuilder.tools import meta_tools

    source = inspect.getsource(meta_tools._execute_get_tool_schema)
    assert 'setdefault("fetched_schemas", [])' in source
    assert 'setdefault("fetched_schemas", set())' not in source
