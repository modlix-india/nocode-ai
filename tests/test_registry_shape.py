"""Broad introspection tests over the appbuilder tool registry.

These tests cover the entire ALL_TOOLS surface — they don't exercise behaviour,
they only assert that every tool's declaration is well-formed enough to be
dispatched and rendered as a provider tool schema.
"""

from __future__ import annotations

import importlib
import inspect

import pytest


KNOWN_PARAM_TYPES = {"string", "integer", "number", "boolean", "array", "object"}

MODLIX_SUBMODULES = [
    "infra",
    "components",
    "pages",
    "kirun",
    "kirun_events",
    "schemas",
    "visuals",
    "visuals_browser",
    "image_ops",
    "security",
    "app_admin",
    "messaging",
    "runtime",
]


def test_all_tools_loads():
    from app.agents.appbuilder.tools.registry import ALL_TOOLS

    assert isinstance(ALL_TOOLS, list)
    assert len(ALL_TOOLS) > 0


def test_no_duplicate_tool_names():
    from app.agents.appbuilder.tools.registry import ALL_TOOLS

    names = [t.name for t in ALL_TOOLS]
    seen: dict[str, int] = {}
    for n in names:
        seen[n] = seen.get(n, 0) + 1
    duplicates = {n: c for n, c in seen.items() if c > 1}
    assert not duplicates, f"Duplicate tool names: {duplicates}"


def test_every_tool_has_name_and_description():
    from app.agents.appbuilder.tools.registry import ALL_TOOLS

    for tool in ALL_TOOLS:
        assert isinstance(tool.name, str) and tool.name.strip(), (
            f"Tool has empty name: {tool!r}"
        )
        assert isinstance(tool.description, str) and tool.description.strip(), (
            f"Tool {tool.name} has empty description"
        )


def test_every_required_param_has_description():
    from app.agents.appbuilder.tools.registry import ALL_TOOLS

    for tool in ALL_TOOLS:
        for param in tool.parameters:
            if param.required:
                assert (
                    isinstance(param.description, str) and param.description.strip()
                ), f"Tool {tool.name} required param {param.name} has empty description"


def test_every_param_type_is_known():
    from app.agents.appbuilder.tools.registry import ALL_TOOLS

    for tool in ALL_TOOLS:
        for param in tool.parameters:
            assert param.type in KNOWN_PARAM_TYPES, (
                f"Tool {tool.name} param {param.name} has unknown type {param.type!r}"
            )


def test_every_tool_execute_is_async_callable():
    from app.agents.appbuilder.tools.registry import ALL_TOOLS

    for tool in ALL_TOOLS:
        # Provider-executed builtin tools (e.g. web_search) legitimately have
        # execute=None — they're handled by the provider, not us. Skip them.
        if tool.builtin_spec is not None:
            continue
        assert tool.execute is not None, f"Tool {tool.name} has execute=None"
        assert callable(tool.execute), f"Tool {tool.name} execute is not callable"
        assert inspect.iscoroutinefunction(tool.execute), (
            f"Tool {tool.name} execute is not a coroutine function"
        )


def test_anthropic_schema_renders():
    from app.agents.appbuilder.tools.registry import ALL_TOOLS

    for tool in ALL_TOOLS:
        schema = tool.to_anthropic_tool()
        assert isinstance(schema, dict), f"Tool {tool.name} schema is not a dict"
        assert "name" in schema, f"Tool {tool.name} schema missing 'name'"
        # builtin tools surface a different (marker) shape; both shapes carry name
        if schema.get("__builtin__"):
            continue
        assert "description" in schema, f"Tool {tool.name} schema missing 'description'"
        assert "input_schema" in schema, (
            f"Tool {tool.name} schema missing 'input_schema'"
        )


def test_modlix_tools_count_floor():
    from app.agents.appbuilder.tools.registry import MODLIX_TOOLS

    assert len(MODLIX_TOOLS) >= 180, (
        f"MODLIX_TOOLS has {len(MODLIX_TOOLS)} tools; expected at least 180"
    )


@pytest.mark.parametrize("submodule", MODLIX_SUBMODULES)
def test_modlix_tools_count_per_module(submodule):
    mod = importlib.import_module(
        f"app.agents.appbuilder.tools.modlix.{submodule}"
    )
    tools = getattr(mod, "TOOLS", None)
    assert tools is not None, f"modlix.{submodule} does not export TOOLS"
    assert isinstance(tools, list), f"modlix.{submodule}.TOOLS is not a list"
    assert len(tools) > 0, f"modlix.{submodule}.TOOLS is empty"
