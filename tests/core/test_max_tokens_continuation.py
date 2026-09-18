"""A turn cut off at the output ceiling is resumed, not silently abandoned.

Found live (2026-09-17, prod session HHARS1_984fdbf5): the agent "just stopped
generating" eight times across nineteen turns. Every stall was an LLM call
returning exactly 16,000 output tokens, which is `AGENT_MAX_TOKENS` hit dead on,
and every one of them was the last call of its turn, followed by the user typing
"continue" / "bro continue" / "hey u stoped like 5 times till now".

Two faults compounded:

  * The OpenAI-compatible providers mapped every finish_reason that was not
    "tool_calls" to "end_turn", so truncation ("length") was indistinguishable
    from a finished answer. `_run_loop`'s `stop_reason == "max_tokens"` warning
    was dead code on DeepSeek, and prod persists no app logs, so a stall left no
    trace anywhere.
  * The loop breaks on any stop_reason that is not "tool_use". A truncated turn
    therefore ended the run mid-sentence with nothing said to the user.

On a thinking model this is not an edge case: reasoning_content is billed as
output and comes out of the same ceiling, so a turn can spend the whole budget
deliberating and stop before writing a single tool call (turn 11 of that
session: 15,999 output tokens for 399 characters of text).

What these lock in: the mapping, the resume, its bound, that partial tool calls
are dropped rather than executed with empty arguments, and that the user is told.
"""

from __future__ import annotations

import unittest

from app.core.agent import (
    _TRUNCATION_EXHAUSTED,
    _TRUNCATION_NOTICE,
    _TRUNCATION_RESUME,
    _TRUNCATED_PLACEHOLDER,
    BaseAgent,
)
from app.core.session import BaseSession
from app.core.streaming import AgentEventStream, AgentEventType
from app.core.tools.base import ToolDefinition, ToolResult
from app.services.llm_provider import StreamChunk, _stop_reason_from_finish


# ── The provider mapping ────────────────────────────────────────────


class FinishReasonMappingTests(unittest.TestCase):
    def test_length_is_max_tokens_not_end_turn(self):
        """The bug, in one assertion."""
        self.assertEqual(_stop_reason_from_finish("length"), "max_tokens")

    def test_tool_calls_still_means_tool_use(self):
        self.assertEqual(_stop_reason_from_finish("tool_calls"), "tool_use")

    def test_stop_is_end_turn(self):
        self.assertEqual(_stop_reason_from_finish("stop"), "end_turn")

    def test_unknown_and_missing_default_to_end_turn(self):
        # An unrecognised reason must not read as "resume me" — that would loop
        # the turn on providers whose vocabulary we don't know.
        self.assertEqual(_stop_reason_from_finish("content_filter"), "end_turn")
        self.assertEqual(_stop_reason_from_finish(None), "end_turn")


class _CapturingClient:
    """Stands in for the OpenAI client, recording the request kwargs. An empty
    stream is enough: the assertion is about what was ASKED for."""

    def __init__(self) -> None:
        self.kwargs: dict = {}
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.kwargs = kwargs
                return iter(())

        self.chat = type("_Chat", (), {"completions": _Completions()})()


class ThinkingFloorTests(unittest.IsolatedAsyncioTestCase):
    """The floor existed on the non-streaming call only, and the agent loop runs
    on the streaming one — so the tier that spends part of its budget on
    reasoning was the single path that never got the headroom for it."""

    async def _request_kwargs(self, *, thinking: bool, max_tokens: int) -> dict:
        import types

        from app.services.llm_provider import DeepSeekProvider

        provider = DeepSeekProvider.__new__(DeepSeekProvider)
        provider.client = _CapturingClient()
        provider.settings = types.SimpleNamespace(DEEPSEEK_THINKING_ENABLED=thinking)
        provider._models = {"fast": "deepseek-flash", "balanced": "deepseek-flash"}

        stream = provider.stream_completion_with_tools(
            system_prompt="sys", messages=[{"role": "user", "content": "hi"}],
            tools=[], model_tier="balanced", max_tokens=max_tokens,
        )
        async for _ in stream:
            pass
        return provider.client.kwargs

    async def test_a_thinking_tier_gets_the_floor(self):
        kwargs = await self._request_kwargs(thinking=True, max_tokens=8000)
        self.assertEqual(kwargs["max_tokens"], 16384)

    async def test_a_budget_above_the_floor_is_left_alone(self):
        kwargs = await self._request_kwargs(thinking=True, max_tokens=32000)
        self.assertEqual(kwargs["max_tokens"], 32000)

    async def test_no_floor_without_thinking(self):
        kwargs = await self._request_kwargs(thinking=False, max_tokens=8000)
        self.assertEqual(kwargs["max_tokens"], 8000)


# ── The loop ────────────────────────────────────────────────────────


class ScriptedProvider:
    """Replays canned turns, recording what it was shown each time."""

    name = "Scripted"

    def __init__(self, turns: list[list[StreamChunk]]) -> None:
        self._turns = turns
        self.calls: list[list[dict]] = []
        # Called with the call number as the turn is issued; how a test
        # simulates something happening while the model was writing.
        self.on_call = None

    def get_model(self, tier: str) -> str:
        return "scripted-1"

    async def stream_completion_with_tools(self, *, messages, **kwargs):
        self.calls.append([
            {**m, "content": [dict(b) for b in m["content"]]
             if isinstance(m["content"], list) else m["content"]}
            for m in messages
        ])
        if self.on_call:
            self.on_call(len(self.calls))
        # Past the script, the model has nothing more to say. Keeps a runaway
        # resume loop from IndexError-ing instead of failing the assertion.
        index = min(len(self.calls) - 1, len(self._turns) - 1)
        for chunk in self._turns[index]:
            yield chunk


USAGE = {"input_tokens": 10, "output_tokens": 16000}


def _truncated_turn(text: str = "Here is the plan:") -> list[StreamChunk]:
    return [
        StreamChunk(type="text_delta", text=text),
        StreamChunk(type="done", stop_reason="max_tokens", usage=dict(USAGE)),
    ]


def _truncated_mid_tool_call() -> list[StreamChunk]:
    """Cut while the arguments were still streaming: the JSON is half a
    document, which the assembler parses to `input: {}`."""
    return [
        StreamChunk(type="tool_use_start", tool_id="t9", tool_name="build"),
        StreamChunk(type="tool_input_delta", tool_id="t9", tool_input_json='{"page": "arse'),
        StreamChunk(type="tool_use_end", tool_id="t9"),
        StreamChunk(type="done", stop_reason="max_tokens", usage=dict(USAGE)),
    ]


def _reasoning_only_truncation() -> list[StreamChunk]:
    """The whole budget spent thinking — no text at all (turn 11 of the prod
    session). An assistant message with empty content is rejected by the APIs."""
    return [
        StreamChunk(type="reasoning_delta", text="thinking " * 50),
        StreamChunk(type="done", stop_reason="max_tokens", usage=dict(USAGE)),
    ]


def _text_turn(text: str) -> list[StreamChunk]:
    return [
        StreamChunk(type="text_delta", text=text),
        StreamChunk(type="done", stop_reason="end_turn",
                    usage={"input_tokens": 10, "output_tokens": 5}),
    ]


def _tool_turn(tool_name: str = "build", tool_id: str = "t1") -> list[StreamChunk]:
    return [
        StreamChunk(type="tool_use_start", tool_id=tool_id, tool_name=tool_name),
        StreamChunk(type="tool_input_delta", tool_id=tool_id, tool_input_json='{"page": "arsenal"}'),
        StreamChunk(type="tool_use_end", tool_id=tool_id),
        StreamChunk(type="done", stop_reason="tool_use",
                    usage={"input_tokens": 10, "output_tokens": 5}),
    ]


class _Auth:
    user_id = "u1"
    client_code = "SYSTEM"
    app_code = "testapp"


class _Context:
    def build_system_prompt(self, dynamic_context: str = "") -> list[dict]:
        return [{"type": "text", "text": "sys"}]


class _Session(BaseSession):
    """A real BaseSession with every DB call stubbed out."""

    def __init__(self) -> None:
        super().__init__(agent_name="test")
        self.session_id = "s1"
        self.auth = _Auth()
        self.persisted: list[tuple[str, str]] = []

    async def set_processing(self) -> None:
        pass

    async def persist_turn(self, user_text, assistant_summary, tool_calls=None, model=None):
        self.persisted.append((user_text, assistant_summary))

    async def persist_turn_incremental(self, user_text, assistant_summary, tool_calls=None, model=None):
        pass

    async def record_token_usage(self, *a, **k):
        pass

    async def save_context(self) -> None:
        pass

    async def complete(self) -> None:
        pass


class _Agent(BaseAgent):
    """BaseAgent with everything outside the loop itself stubbed."""

    def __init__(self) -> None:
        super().__init__(
            name="test",
            tools=[ToolDefinition(name="build", description="d")],
            context_builder=_Context(),
        )
        self.executed: list[dict] = []

    async def _execute_tool(self, tool_name, tool_input, *a, **k):
        self.executed.append({"name": tool_name, "input": tool_input})
        return ToolResult(success=True, summary="built")

    async def build_dynamic_context(self, session) -> str:
        return ""

    async def _observe_to_lore(self, *a, **k):
        pass

    async def _on_loop_complete(self, *a, **k):
        pass

    async def _lore_turn_context(self, session) -> str:
        return ""


def _texts(stream: AgentEventStream) -> list[str]:
    out = []
    while not stream._queue.empty():
        item = stream._queue.get_nowait()
        if getattr(item, "event", None) == AgentEventType.TEXT:
            out.append(item.data["text"])
    return out


def _flatten(content) -> str:
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content if isinstance(b, dict))


class TruncationContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import app.core.agent as agent_module
        from app.services import billing

        self._agent_module = agent_module
        self._billing = billing
        self._saved = (
            agent_module.get_llm_provider,
            billing.check_serving_status,
            billing.charge_llm_call,
        )

        async def _serving(*a, **k):
            return True

        async def _charge(*a, **k):
            return None

        billing.check_serving_status = _serving
        billing.charge_llm_call = _charge

    async def asyncTearDown(self):
        provider, serving, charge = self._saved
        self._agent_module.get_llm_provider = provider
        self._billing.check_serving_status = serving
        self._billing.charge_llm_call = charge

    def _install(self, provider: ScriptedProvider) -> None:
        self._agent_module.get_llm_provider = lambda *a, **k: provider

    async def test_a_truncated_turn_is_resumed_not_ended(self):
        """The whole fix: the run continues instead of handing the user half a
        sentence and going quiet."""
        provider = ScriptedProvider([
            _truncated_turn("Now the rebuild — 30 thin single-glyph streams"),
            _text_turn("Done."),
        ])
        self._install(provider)
        agent, session, stream = _Agent(), _Session(), AgentEventStream()

        await agent._run_loop("make the rain real", session, stream)

        self.assertEqual(len(provider.calls), 2, "the turn was not resumed")
        tail = provider.calls[1][-1]
        self.assertEqual(tail["role"], "user")
        self.assertIn("cut off at the output length limit", _flatten(tail["content"]))

    async def test_the_user_is_told(self):
        provider = ScriptedProvider([_truncated_turn(), _text_turn("Done.")])
        self._install(provider)
        agent, session, stream = _Agent(), _Session(), AgentEventStream()

        await agent._run_loop("build it", session, stream)

        self.assertIn(_TRUNCATION_NOTICE, _texts(stream))

    async def test_the_saved_summary_records_the_cut_off(self):
        """Diagnosing the prod session needed per-call token rows, because
        nothing in the transcript said the turn had been truncated."""
        provider = ScriptedProvider([_truncated_turn(), _text_turn("Done.")])
        self._install(provider)
        agent, session, stream = _Agent(), _Session(), AgentEventStream()

        await agent._run_loop("build it", session, stream)

        self.assertIn(_TRUNCATION_NOTICE.strip(), session.persisted[-1][1])

    async def test_the_truncated_text_is_kept(self):
        """What the model did write stays in the conversation — resuming must
        not cost the user the half-answer they already read."""
        provider = ScriptedProvider([
            _truncated_turn("One write per page:"), _text_turn("Done."),
        ])
        self._install(provider)
        agent, session, stream = _Agent(), _Session(), AgentEventStream()

        await agent._run_loop("build it", session, stream)

        shown = " ".join(_flatten(m["content"]) for m in provider.calls[1])
        self.assertIn("One write per page:", shown)

    async def test_partial_tool_calls_are_dropped_not_executed(self):
        """Half-streamed argument JSON parses to `input: {}`. Executing that
        runs the tool with no arguments, against whatever it defaults to."""
        provider = ScriptedProvider([_truncated_mid_tool_call(), _text_turn("Done.")])
        self._install(provider)
        agent, session, stream = _Agent(), _Session(), AgentEventStream()

        await agent._run_loop("build it", session, stream)

        self.assertEqual(agent.executed, [], "ran a tool call that was cut in half")
        # And nothing dangles: an assistant tool_call with no matching result is
        # rejected by every OpenAI-compatible API.
        for message in provider.calls[1]:
            if message["role"] == "assistant" and isinstance(message["content"], list):
                kinds = [b.get("type") for b in message["content"]]
                self.assertNotIn("tool_use", kinds)

    async def test_a_reasoning_only_truncation_still_leaves_a_valid_message(self):
        """No text at all: the budget went entirely on reasoning. An assistant
        message with empty content is rejected, so a placeholder stands in."""
        provider = ScriptedProvider([_reasoning_only_truncation(), _text_turn("Done.")])
        self._install(provider)
        agent, session, stream = _Agent(), _Session(), AgentEventStream()

        await agent._run_loop("build it", session, stream)

        assistant = [m for m in provider.calls[1] if m["role"] == "assistant"]
        self.assertTrue(assistant)
        for message in assistant:
            self.assertTrue(message["content"], "empty assistant message sent to the API")
        self.assertIn(_TRUNCATED_PLACEHOLDER, _flatten(assistant[0]["content"]))

    async def test_resumes_are_bounded(self):
        """A model that truncates every time must not spend the whole turn
        budget, and the wallet, restarting the same answer."""
        provider = ScriptedProvider([_truncated_turn()])   # truncates forever
        self._install(provider)
        agent, session, stream = _Agent(), _Session(), AgentEventStream()

        await agent._run_loop("build it", session, stream)

        from app.config import settings

        self.assertEqual(
            len(provider.calls), settings.AGENT_MAX_TRUNCATION_CONTINUATIONS + 1,
            "resume bound not honoured",
        )
        self.assertIn(_TRUNCATION_EXHAUSTED, _texts(stream))

    async def test_a_stopped_run_is_not_resumed(self):
        """Stop wins over the resume, as it does over every other continue: the
        user pressed stop while that truncated answer was streaming."""
        provider = ScriptedProvider([_truncated_turn()])
        self._install(provider)
        agent, session, stream = _Agent(), _Session(), AgentEventStream()
        provider.on_call = lambda n: stream.cancel()

        await agent._run_loop("build it", session, stream)

        self.assertEqual(len(provider.calls), 1)
        self.assertNotIn(_TRUNCATION_NOTICE, _texts(stream))

    async def test_an_ordinary_finish_is_untouched(self):
        provider = ScriptedProvider([_text_turn("All done.")])
        self._install(provider)
        agent, session, stream = _Agent(), _Session(), AgentEventStream()

        await agent._run_loop("build it", session, stream)

        self.assertEqual(len(provider.calls), 1)
        self.assertNotIn(_TRUNCATION_NOTICE, _texts(stream))
        self.assertNotIn(_TRUNCATION_RESUME, " ".join(
            _flatten(m["content"]) for m in provider.calls[0]))

    async def test_a_tool_turn_is_untouched(self):
        provider = ScriptedProvider([_tool_turn(), _text_turn("Done.")])
        self._install(provider)
        agent, session, stream = _Agent(), _Session(), AgentEventStream()

        await agent._run_loop("build it", session, stream)

        self.assertEqual([t["name"] for t in agent.executed], ["build"])
        self.assertEqual(agent.executed[0]["input"], {"page": "arsenal"})


if __name__ == "__main__":
    unittest.main()
