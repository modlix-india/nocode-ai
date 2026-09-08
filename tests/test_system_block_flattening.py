"""The system-prompt flatten every non-Anthropic provider goes through.

Anthropic takes the system prompt as a list of content blocks. Every other
provider needs it collapsed into one string, and each of them used to spell
that collapse out itself — four copies, all joining with a single space. A
space join welds each block's opening heading onto the previous block's last
line, which matters more now that `BaseContext` emits three blocks (persona +
tool index, catalogs, per-session context) rather than two.

These lock in the separator and the single implementation, because the
separator is part of the cached prefix on providers that do automatic prefix
caching: if two providers disagree about it, or one drifts back to a space,
the damage is silent.
"""

from __future__ import annotations

import inspect

from app.services import llm_provider
from app.services.llm_provider import (
    DeepSeekProvider,
    GeminiProvider,
    MiniMaxProvider,
    OpenAIProvider,
    flatten_system_blocks,
)


BLOCKS = [
    {"type": "text", "text": "PERSONA\n\n## Available tools\n\n- `x` — y"},
    {"type": "text", "text": "## Component Catalog\n\nTypes: Grid, Text"},
    {"type": "text", "text": "Current session:\n- App: demo"},
]


def test_blocks_are_separated_by_a_blank_line() -> None:
    out = flatten_system_blocks(BLOCKS)
    assert out == (
        "PERSONA\n\n## Available tools\n\n- `x` — y"
        "\n\n"
        "## Component Catalog\n\nTypes: Grid, Text"
        "\n\n"
        "Current session:\n- App: demo"
    )


def test_headings_survive_the_join() -> None:
    """The regression itself: a heading must start its own line."""
    out = flatten_system_blocks(BLOCKS)
    assert "\n\n## Component Catalog" in out
    assert " ## Component Catalog" not in out, (
        "blocks were glued with a space — the catalog heading is no longer at "
        "the start of a line"
    )


def test_plain_string_passes_through() -> None:
    assert flatten_system_blocks("just a string") == "just a string"


def test_none_and_empty_are_safe() -> None:
    assert flatten_system_blocks(None) == ""
    assert flatten_system_blocks([]) == ""


def test_non_text_blocks_are_dropped() -> None:
    mixed = [{"type": "text", "text": "keep"}, {"type": "image", "source": {}}]
    assert flatten_system_blocks(mixed) == "keep"


def test_cache_control_does_not_leak_into_the_text() -> None:
    """Cached blocks carry cache_control; only their text may reach the wire."""
    cached = [{"type": "text", "text": "A", "cache_control": {"type": "ephemeral"}}]
    assert flatten_system_blocks(cached) == "A"


def test_every_provider_flattens_through_the_shared_helper() -> None:
    """No provider may re-implement the join with its own separator.

    Scans each provider class wholesale rather than naming methods, so a new
    entry point that flattens the system prompt is covered the day it lands.
    """
    for cls in (OpenAIProvider, GeminiProvider, DeepSeekProvider):
        src = inspect.getsource(cls)
        assert "flatten_system_blocks" in src, (
            f"{cls.__name__} never calls flatten_system_blocks()"
        )
        assert 'block.get("text", "") for block in system_prompt' not in src, (
            f"{cls.__name__} re-implements the system-block join inline — "
            "call flatten_system_blocks() instead"
        )
        assert 'b.get("text", "") for b in system_prompt' not in src, (
            f"{cls.__name__} re-implements the system-block join inline — "
            "call flatten_system_blocks() instead"
        )


def test_minimax_inherits_the_deepseek_flatten() -> None:
    """MiniMax subclasses DeepSeek; it must not have forked the conversion."""
    assert MiniMaxProvider.stream_completion_with_tools is (
        DeepSeekProvider.stream_completion_with_tools
    )


def test_helper_is_module_level_not_a_method() -> None:
    """Module-level so the Anthropic path and tests can reach it too."""
    assert callable(getattr(llm_provider, "flatten_system_blocks", None))
