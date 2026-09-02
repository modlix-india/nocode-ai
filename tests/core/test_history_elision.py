"""Conversation history must stay inside the context window.

There is no context management on the path the AppBuilder actually runs:
`context_management` is an Anthropic-only server-side beta, it is not configured
for this agent, and the OpenAI-compatible providers ignore the parameter. So on
DeepSeek history grew unbounded — the Chit Fund run sat at context_percent 100
against a 112K window and stopped with no closing summary, and measured per-turn
latency rose from ~4.5s on short conversations to ~19s on the long ones purely
from prefill growth (99% of bench wall-clock is LLM streaming).

Old tool_result payloads are the bulk: 4K each by default, 32K for decompiles,
plus screenshot images. These tests pin the elision's shape, and especially what
it must NOT touch.
"""

from __future__ import annotations

import pytest

from app.core.session import BaseSession


def _session_with(n_turns: int, result_chars: int = 5000) -> BaseSession:
    s = BaseSession(agent_name="test")
    s.messages = [{"role": "user", "content": "build me a thing"}]
    for i in range(n_turns):
        s.messages.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"t{i}", "name": "get_page", "input": {}},
        ]})
        s.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": "P" * result_chars},
        ]})
    return s


# ── it only fires when it needs to ─────────────────────────────────────────


def test_short_history_is_untouched():
    s = _session_with(3)
    before = s.history_chars()
    assert s.elide_old_tool_results(over_chars=200_000) == 0
    assert s.history_chars() == before


def test_disabled_by_zero_threshold():
    s = _session_with(40)
    assert s.elide_old_tool_results(over_chars=0) == 0


def test_a_long_history_is_shrunk():
    s = _session_with(40, result_chars=6000)
    before = s.history_chars()
    freed = s.elide_old_tool_results(keep_recent_turns=6, over_chars=100_000)
    assert freed > 0
    assert s.history_chars() < before


# ── what it must not touch ─────────────────────────────────────────────────


def test_the_recent_window_is_kept_whole():
    """Eliding a result the model is about to use costs a re-fetch turn."""
    s = _session_with(40, result_chars=6000)
    s.elide_old_tool_results(keep_recent_turns=6, over_chars=100_000)
    intact = [b for m in s.messages if isinstance(m.get("content"), list)
              for b in m["content"]
              if isinstance(b, dict) and b.get("type") == "tool_result"
              and not b.get("_elided")]
    # The last 6 assistant turns keep their results; nothing older does.
    assert len(intact) >= 5


def test_every_tool_result_block_survives_as_a_block():
    """A tool_use with no matching tool_result makes the next request invalid."""
    s = _session_with(30, result_chars=6000)
    uses = sum(1 for m in s.messages if isinstance(m.get("content"), list)
               for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use")
    s.elide_old_tool_results(keep_recent_turns=4, over_chars=50_000)
    results = sum(1 for m in s.messages if isinstance(m.get("content"), list)
                  for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result")
    assert results == uses


def test_user_messages_and_assistant_text_are_never_elided():
    s = _session_with(30, result_chars=6000)
    s.messages.insert(1, {"role": "assistant", "content": [
        {"type": "text", "text": "T" * 9000}]})
    s.messages.insert(1, {"role": "user", "content": "U" * 9000})
    s.elide_old_tool_results(keep_recent_turns=4, over_chars=50_000)
    assert any(m.get("content") == "U" * 9000 for m in s.messages)
    assert any(isinstance(m.get("content"), list)
               and any(b.get("text") == "T" * 9000 for b in m["content"] if isinstance(b, dict))
               for m in s.messages)


def test_small_results_are_left_alone():
    """They're cheap, and they're the ones holding ids and keys."""
    s = _session_with(60, result_chars=80)
    s.messages.append({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "big", "content": "X" * 40_000}]})
    s.elide_old_tool_results(keep_recent_turns=2, over_chars=1000, min_result_chars=1500)
    smalls = [b for m in s.messages if isinstance(m.get("content"), list)
              for b in m["content"]
              if isinstance(b, dict) and b.get("type") == "tool_result" and len(str(b.get("content"))) < 200]
    assert all(not b.get("_elided") for b in smalls)


# ── the stub itself ────────────────────────────────────────────────────────


def test_the_stub_keeps_a_head_and_says_how_to_recover():
    s = BaseSession(agent_name="test")
    s.messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "get_page", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "IMPORTANT-HEAD " + "Z" * 50_000}]},
    ] + [m for i in range(8) for m in (
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        {"role": "user", "content": "next"},
    )]
    s.elide_old_tool_results(keep_recent_turns=2, over_chars=1000)
    stub = s.messages[2]["content"][0]["content"]
    assert "IMPORTANT-HEAD" in stub          # a head survives
    assert "elided" in stub                   # it says what happened
    assert "Re-run the tool" in stub          # and how to recover
    assert len(stub) < 1000


def test_a_second_pass_is_free():
    s = _session_with(40, result_chars=6000)
    first = s.elide_old_tool_results(keep_recent_turns=4, over_chars=50_000)
    second = s.elide_old_tool_results(keep_recent_turns=4, over_chars=50_000)
    assert first > 0
    assert second == 0


# ── sizing ─────────────────────────────────────────────────────────────────


def test_image_payloads_are_counted_by_their_base64_weight():
    """A screenshot dwarfs the text beside it, so sizing must see it."""
    s = BaseSession(agent_name="test")
    s.messages = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "x", "content": [
            {"type": "text", "text": "shot"},
            {"type": "image", "source": {"type": "base64", "data": "D" * 30_000}},
        ]},
    ]}]
    assert s.history_chars() > 30_000


def test_history_chars_tolerates_junk_shapes():
    s = BaseSession(agent_name="test")
    s.messages = [{"role": "user", "content": None}, {"role": "user"}, {"role": "user", "content": 42}]
    assert s.history_chars() == 0


def test_the_loop_calls_it_every_turn():
    """Wired into `_run_loop`, which is where the per-turn work happens."""
    import inspect
    from app.core.agent import BaseAgent
    src = inspect.getsource(BaseAgent._run_loop)
    assert "elide_old_tool_results" in src
    # Must sit with the turn increment, not after the LLM call, or the turn that
    # overflows the window is the one that pays for it.
    assert src.index("turn += 1") < src.index("elide_old_tool_results")
