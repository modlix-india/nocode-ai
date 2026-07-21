"""Regression: Gemini's `function_call.args` payload must be fully converted
from proto-plus types (MapComposite, RepeatedComposite) to plain Python
before reaching `json.dumps` or session persistence.

Bench discovery (2026-06-10): Gemini provider crashed in
stream_completion_with_tools with
"TypeError: Object of type MapComposite is not JSON serializable" at
[app/services/llm_provider.py:387](app/services/llm_provider.py#L387) where
`json_lib.dumps(block["input"])` was called on a tool-input that still had
nested proto-plus types inside.

Root cause: the previous fix at `_gemini_part_to_anthropic_block` only did
`dict(fc.args)` — a one-level conversion. When the LLM returned a tool call
with nested-object arguments (e.g. `properties={"foo": {...}}`), the inner
dict was still a `MapComposite` because Gemini's proto-plus types nest.

Fix: a recursive `GeminiProvider._proto_to_plain(value)` walker that flattens
the entire tree to plain Python primitives at response-shaping time.

These tests lock in:
  - Top-level dict-shaped args are converted
  - Nested dicts are converted recursively (the actual bug)
  - Lists / tuples are converted (RepeatedComposite path)
  - Primitives pass through unchanged
  - The output JSON-serializes cleanly (the contract the bench enforces)
  - Unknown proto types fall back to str() rather than crashing the agent
"""

from __future__ import annotations

import json

import pytest

from app.services.llm_provider import GeminiProvider


# ── Fake proto types matching what google.generativeai actually returns ─────


class _FakeMapComposite:
    """Stand-in for proto-plus MapComposite — iterable like a dict but not
    a dict subclass. Mirrors the actual library shape that broke us."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def items(self):
        return self._data.items()

    def __iter__(self):
        return iter(self._data)

    def __getitem__(self, key):
        return self._data[key]


class _FakeRepeatedComposite:
    """Stand-in for proto-plus RepeatedComposite — iterable like a list."""

    def __init__(self, data: list) -> None:
        self._data = data

    def __iter__(self):
        return iter(self._data)


# ── _proto_to_plain unit tests ──────────────────────────────────────────────


def test_proto_to_plain_passes_through_primitives() -> None:
    assert GeminiProvider._proto_to_plain("hello") == "hello"
    assert GeminiProvider._proto_to_plain(42) == 42
    assert GeminiProvider._proto_to_plain(3.14) == 3.14
    assert GeminiProvider._proto_to_plain(True) is True
    assert GeminiProvider._proto_to_plain(None) is None


def test_proto_to_plain_converts_plain_dict() -> None:
    assert GeminiProvider._proto_to_plain({"a": 1, "b": "x"}) == {"a": 1, "b": "x"}


def test_proto_to_plain_converts_plain_list() -> None:
    assert GeminiProvider._proto_to_plain([1, 2, 3]) == [1, 2, 3]


def test_proto_to_plain_converts_map_composite() -> None:
    """The simple case: a top-level MapComposite arrives — convert to dict."""
    mc = _FakeMapComposite({"page_name": "home", "size": 100})
    result = GeminiProvider._proto_to_plain(mc)
    assert result == {"page_name": "home", "size": 100}
    assert isinstance(result, dict)


def test_proto_to_plain_recurses_into_nested_map_composite() -> None:
    """The actual bug: nested MapComposite was the JSON crash culprit. A
    top-level dict({"properties": MapComposite({...})}) survived the previous
    fix because dict(...) only flattens one layer."""
    nested = _FakeMapComposite({
        "outer": "ok",
        "properties": _FakeMapComposite({
            "foo": _FakeMapComposite({"type": "string", "description": "the foo field"}),
            "bar": _FakeMapComposite({"type": "integer"}),
        }),
    })
    result = GeminiProvider._proto_to_plain(nested)
    assert result == {
        "outer": "ok",
        "properties": {
            "foo": {"type": "string", "description": "the foo field"},
            "bar": {"type": "integer"},
        },
    }
    # Critically: the whole thing must JSON-serialize. This is the contract
    # the agent loop relies on at llm_provider.py:387.
    serialized = json.dumps(result)
    assert "properties" in serialized
    assert "the foo field" in serialized


def test_proto_to_plain_recurses_into_repeated_composite() -> None:
    """RepeatedComposite (list-like proto). Same recursion contract."""
    rc = _FakeRepeatedComposite([
        _FakeMapComposite({"name": "a"}),
        _FakeMapComposite({"name": "b"}),
    ])
    result = GeminiProvider._proto_to_plain(rc)
    assert result == [{"name": "a"}, {"name": "b"}]
    json.dumps(result)  # serializable


def test_proto_to_plain_handles_mixed_nesting() -> None:
    """Real-world shape from a Gemini tool call: dict whose values are a mix
    of strings, lists of dicts, and nested dicts."""
    payload = _FakeMapComposite({
        "app_code": "appbuilder",
        "components": _FakeRepeatedComposite([
            _FakeMapComposite({"key": "btn1", "type": "Button"}),
            _FakeMapComposite({"key": "txt1", "type": "TextBox"}),
        ]),
        "config": _FakeMapComposite({
            "theme": _FakeMapComposite({"primary": "#1e88e5"}),
        }),
    })
    result = GeminiProvider._proto_to_plain(payload)
    assert result == {
        "app_code": "appbuilder",
        "components": [
            {"key": "btn1", "type": "Button"},
            {"key": "txt1", "type": "TextBox"},
        ],
        "config": {
            "theme": {"primary": "#1e88e5"},
        },
    }
    json.dumps(result)


def test_proto_to_plain_unknown_type_falls_back_to_str() -> None:
    """An unrecognized proto type must NOT crash the agent — return str() so
    the LLM at least sees something semantic and we don't lose the turn."""
    class _Weird:
        def __repr__(self) -> str:
            return "<custom proto thing>"

    out = GeminiProvider._proto_to_plain(_Weird())
    assert out == "<custom proto thing>"
    json.dumps(out)


# ── End-to-end via _gemini_part_to_anthropic_block ─────────────────────────


class _FakeFC:
    """Stand-in for a Gemini function_call part."""
    def __init__(self, name: str, args) -> None:
        self.name = name
        self.args = args


class _FakePart:
    def __init__(self, text=None, function_call=None) -> None:
        self.text = text
        self.function_call = function_call


def test_part_to_block_serializes_after_conversion() -> None:
    """The bench's contract: whatever comes out of _gemini_part_to_anthropic_block
    must round-trip through json.dumps cleanly at llm_provider.py:387."""
    fc = _FakeFC(
        name="patch_component_props",
        args=_FakeMapComposite({
            "page_name": "home",
            "key": "btn1",
            "properties": _FakeMapComposite({
                "label": _FakeMapComposite({"value": "Submit"}),
            }),
        }),
    )
    part = _FakePart(function_call=fc)
    block = GeminiProvider._gemini_part_to_anthropic_block(part)
    assert block is not None
    assert block["type"] == "tool_use"
    assert block["name"] == "patch_component_props"
    # The exact failing call site — must not crash.
    serialized = json.dumps(block["input"])
    assert "btn1" in serialized
    assert "Submit" in serialized


def test_part_to_block_empty_args_becomes_empty_dict() -> None:
    """A function_call with no args should produce input={}."""
    fc = _FakeFC(name="list_pages", args=None)
    part = _FakePart(function_call=fc)
    block = GeminiProvider._gemini_part_to_anthropic_block(part)
    assert block is not None
    assert block["input"] == {}
    json.dumps(block["input"])


def test_part_to_block_text_path_unchanged() -> None:
    """Sanity: the non-function-call path still works."""
    part = _FakePart(text="hello world")
    block = GeminiProvider._gemini_part_to_anthropic_block(part)
    assert block == {"type": "text", "text": "hello world"}
