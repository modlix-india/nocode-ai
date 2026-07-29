"""Regression: DeepSeek V4 Pro requires `reasoning_content` to round-trip.

Bench discovery (2026-06-10): switching `DEEPSEEK_MODEL_BALANCED` to
`deepseek-v4-pro` made every turn-2+ request fail with HTTP 400:
"The `reasoning_content` in the thinking mode must be passed back to the API."

Root cause: V4 Pro is a reasoning model that emits chain-of-thought tokens
in `delta.reasoning_content` during streaming. The API contract requires
that text to be **echoed back** on every subsequent assistant message in
the conversation history. Our DeepSeek provider already had the *send-back*
half wired (llm_provider.py:1293-1296 — reads `_reasoning_content` off the
assistant msg into the OpenAI-shape `reasoning_content` field), but the
*capture* half was missing. Turn 1 worked because there was no prior
assistant message; turn 2+ failed because the prior turn's CoT was gone.

Fix:
  1. Provider's stream loop accumulates `delta.reasoning_content` across
     deltas and stashes the joined string in `done` chunk's usage dict.
  2. Agent loop pops `reasoning_content` from usage and passes it to
     `session.append_assistant_message`, which persists it as
     `_reasoning_content` on the assistant message.
  3. On the next turn, the provider's message-builder finds
     `_reasoning_content` and re-emits it on the assistant message
     (the path already in place).

These tests lock in:
  - The stream loop emits `reasoning_delta` chunks for live UI consumption
  - The done chunk's usage carries the joined `reasoning_content` text
  - The agent loop persists it via session.append_assistant_message
  - Subsequent turns emit `reasoning_content` on the prior assistant msg
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services.llm_provider import DeepSeekProvider, StreamChunk


# ── Stream-capture: reasoning_delta + accumulation onto done chunk ─────────


class _FakeDelta:
    def __init__(self, content=None, reasoning_content=None, tool_calls=None) -> None:
        self.content = content
        self.reasoning_content = reasoning_content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, delta=None, finish_reason=None) -> None:
        self.delta = delta
        self.finish_reason = finish_reason


class _FakeUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeStreamChunk:
    def __init__(self, choices=None, usage=None) -> None:
        self.choices = choices or []
        self.usage = usage


def _make_provider() -> DeepSeekProvider:
    """Construct a provider with a stubbed OpenAI client so we don't need
    a real API key for the unit test. We never call create_completion
    directly — only stream_completion_with_tools, which uses self.client."""
    p = DeepSeekProvider.__new__(DeepSeekProvider)
    p.client = MagicMock()
    # The provider's _models dict is what get_model reads; mirror the
    # production shape.
    p._models = {
        "fast":     "deepseek-v4-flash",
        "balanced": "deepseek-v4-pro",
    }
    # Minimal settings stub used by _is_thinking_tier.
    class _S:
        DEEPSEEK_THINKING_ENABLED = True
    p.settings = _S()
    return p


@pytest.mark.asyncio
async def test_stream_accumulates_reasoning_content_into_done_usage() -> None:
    """The stream loop must accumulate `delta.reasoning_content` across all
    chunks and surface the joined text on the `done` chunk's usage dict."""
    provider = _make_provider()

    fake_chunks = [
        _FakeStreamChunk([_FakeChoice(_FakeDelta(reasoning_content="Let me think "))]),
        _FakeStreamChunk([_FakeChoice(_FakeDelta(reasoning_content="step by step. "))]),
        _FakeStreamChunk([_FakeChoice(_FakeDelta(content="Hello"))]),
        _FakeStreamChunk([_FakeChoice(_FakeDelta(reasoning_content="The answer is X."))]),
        _FakeStreamChunk(
            [_FakeChoice(_FakeDelta(content="!"), finish_reason="stop")],
            usage=_FakeUsage(prompt_tokens=100, completion_tokens=42),
        ),
    ]

    # Stub the sync stream executor to yield our fake chunks directly.
    provider.client.chat.completions.create.return_value = iter(fake_chunks)

    out_chunks: list[StreamChunk] = []
    async for sc in provider.stream_completion_with_tools(
        system_prompt="sys", messages=[{"role": "user", "content": "hi"}],
        tools=[], model_tier="balanced", max_tokens=1024,
    ):
        out_chunks.append(sc)

    # The text content survived
    text = "".join(c.text for c in out_chunks if c.type == "text_delta")
    assert text == "Hello!"

    # reasoning_delta chunks were yielded for live UI
    reasoning_deltas = [c.text for c in out_chunks if c.type == "reasoning_delta"]
    assert reasoning_deltas == ["Let me think ", "step by step. ", "The answer is X."]

    # done chunk carries the joined reasoning_content in its usage dict
    done_chunks = [c for c in out_chunks if c.type == "done"]
    assert len(done_chunks) == 1
    done = done_chunks[0]
    assert done.usage.get("reasoning_content") == "Let me think step by step. The answer is X."
    # Token counts still intact
    assert done.usage.get("input_tokens") == 100
    assert done.usage.get("output_tokens") == 42


@pytest.mark.asyncio
async def test_stream_omits_reasoning_when_provider_emits_none() -> None:
    """A non-thinking response (no `delta.reasoning_content` ever set) must
    NOT include a `reasoning_content` key in usage — otherwise we'd persist
    an empty string and send it back on the next turn for no reason."""
    provider = _make_provider()

    fake_chunks = [
        _FakeStreamChunk([_FakeChoice(_FakeDelta(content="Plain response"))]),
        _FakeStreamChunk(
            [_FakeChoice(_FakeDelta(), finish_reason="stop")],
            usage=_FakeUsage(prompt_tokens=10, completion_tokens=3),
        ),
    ]
    provider.client.chat.completions.create.return_value = iter(fake_chunks)

    out_chunks: list[StreamChunk] = []
    async for sc in provider.stream_completion_with_tools(
        system_prompt="sys", messages=[{"role": "user", "content": "hi"}],
        tools=[], model_tier="balanced", max_tokens=1024,
    ):
        out_chunks.append(sc)

    done = next(c for c in out_chunks if c.type == "done")
    assert "reasoning_content" not in done.usage
    # No phantom reasoning_delta chunks either
    assert not any(c.type == "reasoning_delta" for c in out_chunks)


# ── Agent-loop wiring: agent.py pops reasoning_content from usage ──────────


def test_agent_loop_pops_reasoning_content_from_usage() -> None:
    """The agent loop at app/core/agent.py:run reads `reasoning_content` from
    usage and passes it to session.append_assistant_message. Source-level
    inspect because the loop is async + interleaved with streaming; this
    test prevents accidental reversion to the old `reasoning_content = None`.
    """
    from app.core import agent as core_agent

    source = inspect.getsource(core_agent.BaseAgent._run_loop)
    # Must NOT be the hardcoded-None pattern that caused the original bug.
    assert "reasoning_content = None" not in source, (
        "agent._run_loop still hardcodes reasoning_content=None — DeepSeek V4 Pro "
        "will fail with HTTP 400 on every turn-2+ request. Replace with "
        "`usage.pop('reasoning_content', None)`."
    )
    # Must pop from usage (the keyword `reasoning_content` should appear
    # alongside `pop` somewhere in the function body).
    assert "reasoning_content" in source, "reasoning_content not handled in agent._run_loop"
    assert ".pop(\"reasoning_content\"" in source or ".pop('reasoning_content'" in source, (
        "Expected `usage.pop('reasoning_content', None)` — see "
        "`test_deepseek_reasoning_roundtrip.py` for context."
    )


# ── Session-side round-trip: persistence + replay on next-turn build ───────


def test_session_persists_reasoning_content_as_underscore_field() -> None:
    """`append_assistant_message(content_blocks, reasoning_content)` stores
    the CoT text on the message as `_reasoning_content` (underscore prefix
    matches the provider's read site at llm_provider.py:1294)."""
    from app.core.session import BaseSession

    sess = BaseSession.__new__(BaseSession)
    sess.messages = []
    sess.append_assistant_message(
        [{"type": "text", "text": "answer"}],
        reasoning_content="thinking step by step",
    )
    assert len(sess.messages) == 1
    msg = sess.messages[0]
    assert msg["role"] == "assistant"
    assert msg.get("_reasoning_content") == "thinking step by step"


def test_session_omits_reasoning_field_when_none() -> None:
    """No reasoning_content → no `_reasoning_content` key. Important because
    the provider's send-back path uses `msg.get('_reasoning_content')` and
    a present-but-empty value would emit an invalid (empty) reasoning_content
    field that V4 Pro might also reject."""
    from app.core.session import BaseSession

    sess = BaseSession.__new__(BaseSession)
    sess.messages = []
    sess.append_assistant_message([{"type": "text", "text": "answer"}], None)
    msg = sess.messages[0]
    assert "_reasoning_content" not in msg


# ── End-to-end: stream → agent → next turn build emits reasoning_content ──


def test_provider_message_builder_emits_reasoning_content_on_next_turn() -> None:
    """The provider's full_messages builder must include the prior assistant
    message's `_reasoning_content` as `reasoning_content` when thinking mode
    is active. Inspect the source to anchor the wiring."""
    source = inspect.getsource(DeepSeekProvider.create_completion_with_tools)
    # The send-back path must look up _reasoning_content on assistant msgs.
    assert '_reasoning_content' in source
    assert 'reasoning_content' in source
    # The OpenAI-shape field name used in full_messages.
    assert 'oai_msg["reasoning_content"]' in source or "oai_msg['reasoning_content']" in source


def test_streaming_message_builder_also_emits_reasoning_content() -> None:
    """Bench discovery (2026-06-10, third bench attempt): the agent loop uses
    `stream_completion_with_tools`, not `create_completion_with_tools`. The
    non-streaming path had reasoning_content round-trip wired; the streaming
    path's message-builder was DROPPING `_reasoning_content` on assistant
    messages — turn 2+ failed with HTTP 400 "reasoning_content must be
    passed back".

    Lock both paths in: every path that rebuilds messages for V4 Pro must
    surface _reasoning_content. If a future contributor copies the streaming
    builder to a new path without copying this branch, the bench breaks.
    """
    source = inspect.getsource(DeepSeekProvider.stream_completion_with_tools)
    assert '_reasoning_content' in source, (
        "stream_completion_with_tools dropped _reasoning_content round-trip — "
        "V4 Pro thinking mode multi-turn will fail with HTTP 400."
    )
    assert 'oai_msg["reasoning_content"]' in source or "oai_msg['reasoning_content']" in source
