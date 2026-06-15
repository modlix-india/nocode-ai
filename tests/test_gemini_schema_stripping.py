"""Regression: Gemini tool conversion must strip schema keys Gemini rejects.

Bench discovery (2026-06-06): Gemini provider crashed at
GenerativeModel(...) instantiation with
"ValueError: Unknown field for Schema: default" — every tool that has a
ToolParameter with `default=` (e.g. `size=100`, `max_results=8`,
`lines=200`) made `to_json_schema()` emit `{"default": ...}`, which
Gemini's protobuf Schema doesn't accept. The bench couldn't even
register tools before the LLM call, so every Gemini conversation
failed with ValueError before any progress.

Fix: `_strip_gemini_unsupported_schema_keys` recursively whitelists
Gemini-accepted JSON-Schema keys (type, description, properties, items,
required, enum, format, nullable) and drops everything else.

These tests lock in:
  - The stripper drops `default` at the top level
  - It drops `default` nested inside properties (which is where it
    actually lives for our ToolParameters)
  - It preserves whitelisted keys (type, description, enum, items, required, properties)
  - It doesn't mutate the input
  - Real ALL_TOOLS schemas pass through cleanly
"""

from __future__ import annotations

import pytest

from app.services.llm_provider import (
    GeminiProvider,
    _strip_gemini_unsupported_schema_keys,
    _GEMINI_SCHEMA_ALLOWED_KEYS,
)


def test_strip_drops_default_at_top_level() -> None:
    raw = {"type": "object", "default": "foo", "description": "x"}
    out = _strip_gemini_unsupported_schema_keys(raw)
    assert out == {"type": "object", "description": "x"}


def test_strip_drops_default_inside_properties() -> None:
    """Where `default` actually lives in our ToolParameter schemas."""
    raw = {
        "type": "object",
        "properties": {
            "size": {"type": "integer", "description": "Max rows", "default": 100},
            "name": {"type": "string", "description": "Pattern name"},
        },
        "required": ["name"],
    }
    out = _strip_gemini_unsupported_schema_keys(raw)
    assert out == {
        "type": "object",
        "properties": {
            "size": {"type": "integer", "description": "Max rows"},
            "name": {"type": "string", "description": "Pattern name"},
        },
        "required": ["name"],
    }


def test_strip_drops_unsupported_keys() -> None:
    """Drops `examples`, `$ref`, `oneOf`, `additionalProperties` recursively."""
    raw = {
        "type": "object",
        "examples": [{"x": 1}],
        "additionalProperties": False,
        "properties": {
            "x": {"type": "string", "$ref": "#/defs/x", "examples": ["a"]},
            "y": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
        },
    }
    out = _strip_gemini_unsupported_schema_keys(raw)
    assert "examples" not in out
    assert "additionalProperties" not in out
    assert "$ref" not in out["properties"]["x"]
    assert "examples" not in out["properties"]["x"]
    assert "oneOf" not in out["properties"]["y"]


def test_strip_preserves_whitelisted_keys() -> None:
    raw = {
        "type": "array",
        "description": "List of names",
        "items": {"type": "string", "enum": ["a", "b"]},
    }
    out = _strip_gemini_unsupported_schema_keys(raw)
    assert out == raw  # everything in the whitelist


def test_strip_is_non_mutating() -> None:
    """Stripper must NOT mutate the input — same tool gets shown to
    Anthropic + Gemini in the same process, so the Anthropic shape must
    keep its `default` fields intact."""
    raw = {
        "type": "object",
        "properties": {"size": {"type": "integer", "default": 100}},
    }
    original_repr = repr(raw)
    _ = _strip_gemini_unsupported_schema_keys(raw)
    assert repr(raw) == original_repr  # unchanged


def test_strip_handles_lists_and_primitives() -> None:
    """Schema can have list values (e.g. enum, required); recurse through them."""
    raw = {"type": "string", "enum": ["a", "b", "c"]}
    assert _strip_gemini_unsupported_schema_keys(raw) == raw
    # Primitives pass through unchanged.
    assert _strip_gemini_unsupported_schema_keys("plain string") == "plain string"
    assert _strip_gemini_unsupported_schema_keys(42) == 42


def test_convert_tools_with_default_param_does_not_crash() -> None:
    """End-to-end: a tool with a default param passes through _convert_tools
    cleanly — no ValueError raised. This is the exact path the bench crashed on.

    We import the provider class but don't instantiate it (no GOOGLE_API_KEY
    needed); _convert_tools is a static helper, callable without a model.
    """
    # Synthesize an Anthropic-shape tool with a default in its input_schema.
    tool_in = {
        "name": "list_pages",
        "description": "List pages in an app",
        "input_schema": {
            "type": "object",
            "properties": {
                "app_code": {"type": "string", "description": "App code"},
                "size": {"type": "integer", "description": "Max rows", "default": 200},
            },
            "required": ["app_code"],
        },
    }
    out = GeminiProvider._convert_tools(GeminiProvider, [tool_in])
    assert out  # non-empty list of Tool objects
    assert "function_declarations" in out[0]
    fns = out[0]["function_declarations"]
    assert len(fns) == 1
    params = fns[0]["parameters"]
    # The default field is gone from the size param.
    assert "default" not in params["properties"]["size"]
    # But the rest is intact.
    assert params["properties"]["size"]["type"] == "integer"
    assert params["properties"]["size"]["description"] == "Max rows"
    assert "app_code" in params["required"]


def test_real_all_tools_pass_through_cleanly() -> None:
    """Run the real ALL_TOOLS surface through the converter — none should
    leave a stripped key behind. Catches future tool definitions that
    introduce unsupported JSON-Schema constructs."""
    from app.agents.appbuilder.tools.registry import ALL_TOOLS

    anthropic_tools = [t.to_anthropic_tool() for t in ALL_TOOLS]
    out = GeminiProvider._convert_tools(GeminiProvider, anthropic_tools)
    # Walk every parameter on every function declaration and confirm only
    # whitelisted keys remain.
    bad_keys: list[tuple[str, str, str]] = []  # (tool_name, path, key)
    for tool_obj in out:
        for fn in tool_obj.get("function_declarations") or []:
            name = fn.get("name", "?")
            params = fn.get("parameters") or {}
            _walk_assert_allowed(params, name, "", bad_keys)
    assert not bad_keys, (
        f"{len(bad_keys)} unsupported key(s) survived stripping: {bad_keys[:5]}..."
    )


def _walk_assert_allowed(node, tool_name, path, bad_keys, in_properties_value=False):
    """Helper for the ALL_TOOLS test: walk a schema and collect any key
    not in _GEMINI_SCHEMA_ALLOWED_KEYS.

    Context-aware: when `in_properties_value` is True, the dict keys at
    this level are user-defined property names (not schema keywords) and
    must NOT be checked against the whitelist. Same shape as the stripper.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if in_properties_value:
                # k is a property name (e.g. 'app_code'); only its VALUE is schema.
                _walk_assert_allowed(v, tool_name, f"{path}.{k}", bad_keys, False)
                continue
            if k not in _GEMINI_SCHEMA_ALLOWED_KEYS:
                bad_keys.append((tool_name, path, k))
            # The value of `properties` is a {prop_name: schema} map.
            child_in_props = (k == "properties")
            # `required` / `enum` are plain string-lists; don't recurse-check.
            if k in ("required", "enum"):
                continue
            _walk_assert_allowed(v, tool_name, f"{path}.{k}", bad_keys, child_in_props)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _walk_assert_allowed(item, tool_name, f"{path}[{i}]", bad_keys, in_properties_value)
