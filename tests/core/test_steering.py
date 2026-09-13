"""Steering: messages the user sends while the agent is still working.

POST /chat answers 409 for the whole length of a run, because two agents
interleaving tool calls and history writes on one session corrupt both. So a
mid-run message rides the control channel instead (POST /steer →
stream_registry → AgentEventStream.push_steer) and the loop folds it into the
conversation at its next turn boundary.

What these lock in:

  * Delivery shape: a steer joins the tail user message (the tool_result one)
    rather than becoming a second consecutive user message, which every
    provider rejects.
  * The acknowledgement: a `steer` event with applied=True is emitted ONLY
    when the text actually reached the model, and one with applied=False when
    it never will. The client draws the bubble off the first and hands the text
    back to its input box on the second, so an accepted message can never
    silently vanish.
  * Re-opening: a steer that lands after the model has said its piece runs the
    loop again instead of waiting for the next message. Steering the answer is
    the point; steering only the gaps between tool calls would miss it.
  * Persistence: the saved turn records what was asked INCLUDING the steer.
"""

from __future__ import annotations

import unittest

from app.core import stream_registry
from app.core.agent import BaseAgent
from app.core.session import BaseSession
from app.core.streaming import AgentEventStream, AgentEventType
from app.core.tools.base import ToolDefinition, ToolResult
from app.services.llm_provider import StreamChunk


# ── The queue on the stream ─────────────────────────────────────────


class PushSteerTests(unittest.IsolatedAsyncioTestCase):
    """Async-cased purely so there is a running loop: AgentEventStream builds an
    asyncio.Queue in its constructor, which on 3.9 needs one."""

    async def test_accepts_and_returns_a_stable_id(self):
        stream = AgentEventStream()
        steer_id = stream.push_steer("use the signup page instead")
        self.assertTrue(steer_id)
        self.assertTrue(stream.has_steers)
        self.assertEqual(
            stream.drain_steers(),
            [{"id": steer_id, "text": "use the signup page instead"}],
        )
        self.assertFalse(stream.has_steers)

    async def test_client_supplied_id_wins(self):
        stream = AgentEventStream()
        # The client mints it so its optimistic bubble and the confirming
        # event are the same bubble.
        self.assertEqual(stream.push_steer("hi", "steer_abc"), "steer_abc")

    async def test_blank_is_refused(self):
        stream = AgentEventStream()
        self.assertEqual(stream.push_steer("   "), "")
        self.assertEqual(stream.push_steer(""), "")
        self.assertFalse(stream.has_steers)

    async def test_cancelled_run_takes_nothing(self):
        stream = AgentEventStream()
        stream.cancel()
        self.assertEqual(stream.push_steer("too late"), "")

    async def test_drain_preserves_order(self):
        stream = AgentEventStream()
        stream.push_steer("one")
        stream.push_steer("two")
        self.assertEqual([s["text"] for s in stream.drain_steers()], ["one", "two"])


class RegistrySteerTests(unittest.IsolatedAsyncioTestCase):
    """The control-channel hop: a POST landing on this worker reaches the run."""

    async def asyncTearDown(self):
        stream_registry.unregister("s-steer")

    async def test_local_stream_takes_it(self):
        stream = AgentEventStream()
        stream_registry.register("s-steer", stream)
        delivered = await stream_registry.signal(
            "s-steer", "steer", {"message": "stop, use the draft", "steer_id": "x1"},
        )
        self.assertEqual(delivered, "local")
        self.assertEqual(stream.drain_steers(), [{"id": "x1", "text": "stop, use the draft"}])

    async def test_no_run_is_missing_not_delivered(self):
        # Redis off in tests, so a local miss is the whole answer.
        delivered = await stream_registry.signal("s-nobody", "steer", {"message": "hi"})
        self.assertEqual(delivered, "missing")


# ── Delivery into the conversation ──────────────────────────────────


class AppendUserTextTests(unittest.TestCase):
    def _session(self) -> BaseSession:
        session = BaseSession.__new__(BaseSession)
        session.messages = []
        return session

    def test_joins_the_tool_result_message(self):
        session = self._session()
        result = {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
        session.append_tool_results([result])
        session.append_user_text("actually, make it blue")

        # One message, not two: a second consecutive user message is rejected.
        self.assertEqual(len(session.messages), 1)
        content = session.messages[-1]["content"]
        self.assertEqual(content[0], result)
        self.assertEqual(content[-1], {"type": "text", "text": "actually, make it blue"})

    def test_string_content_is_promoted_to_blocks(self):
        session = self._session()
        session.append_user_message("build a login page")
        session.append_user_text("with Google sign-in")
        self.assertEqual(len(session.messages), 1)
        self.assertEqual(
            session.messages[0]["content"],
            [
                {"type": "text", "text": "build a login page"},
                {"type": "text", "text": "with Google sign-in"},
            ],
        )

    def test_after_an_assistant_message_it_stands_alone(self):
        session = self._session()
        session.append_user_message("build it")
        session.append_assistant_message([{"type": "text", "text": "Done."}])
        session.append_user_text("no, the other one")
        self.assertEqual(len(session.messages), 3)
        self.assertEqual(session.messages[-1]["role"], "user")
        self.assertEqual(
            session.messages[-1]["content"], [{"type": "text", "text": "no, the other one"}],
        )

    def test_empty_text_is_a_no_op(self):
        session = self._session()
        session.append_user_text("")
        self.assertEqual(session.messages, [])


class WithSteerTests(unittest.TestCase):
    def test_appends_for_persistence(self):
        self.assertEqual(BaseAgent._with_steer("build it", "in blue"), "build it\n\nin blue")

    def test_nothing_steered_is_unchanged(self):
        self.assertEqual(BaseAgent._with_steer("build it", ""), "build it")


class TurnUpsertKeepsTheInstructionCurrentTests(unittest.TestCase):
    """Found live (2026-09-13): a steered turn saved the original message only.

    The row is created by the first incremental save, before any steer has
    arrived, and the upsert's ON DUPLICATE KEY UPDATE list did not include
    USER_INSTRUCTION. Nothing had ever changed it mid-turn before steering
    existed, so the omission was invisible. Source-level, because the
    statement only runs against a live MySQL.
    """

    def test_user_instruction_is_in_the_update_list(self):
        import inspect

        from app.services.context_manager import ContextManager

        source = inspect.getsource(ContextManager.upsert_turn)
        update_clause = source.split("ON DUPLICATE KEY UPDATE", 1)
        self.assertEqual(len(update_clause), 2, "upsert_turn no longer upserts")
        self.assertIn("USER_INSTRUCTION = COALESCE(", update_clause[1])
        # NULLIF guards the other direction: a later write with nothing in it
        # must not erase what is already saved.
        self.assertIn("NULLIF(VALUES(USER_INSTRUCTION), '')", update_clause[1])


# ── The loop ────────────────────────────────────────────────────────


class ScriptedProvider:
    """Replays a canned list of turns, recording what it was asked each time."""

    name = "Scripted"

    def __init__(self, turns: list[list[StreamChunk]]) -> None:
        self._turns = turns
        self.calls: list[list[dict]] = []
        # Called by the loop between turns; whatever it returns is pushed onto
        # the stream, which is how a test simulates a message typed mid-run.
        self.on_call = None

    def get_model(self, tier: str) -> str:
        return "scripted-1"

    async def stream_completion_with_tools(self, *, messages, **kwargs):
        # Deep-ish copy: the loop mutates history after the call, and the
        # assertion is about what the model was SHOWN at this moment.
        self.calls.append([{**m, "content": _copy_content(m["content"])} for m in messages])
        if self.on_call:
            self.on_call(len(self.calls))
        for chunk in self._turns[len(self.calls) - 1]:
            yield chunk


def _copy_content(content):
    return [dict(b) for b in content] if isinstance(content, list) else content


def _text_turn(text: str) -> list[StreamChunk]:
    return [
        StreamChunk(type="text_delta", text=text),
        StreamChunk(type="done", stop_reason="end_turn", usage={"input_tokens": 10, "output_tokens": 5}),
    ]


def _tool_turn(tool_name: str, tool_id: str = "t1") -> list[StreamChunk]:
    return [
        StreamChunk(type="tool_use_start", tool_id=tool_id, tool_name=tool_name),
        StreamChunk(type="tool_input_delta", tool_id=tool_id, tool_input_json="{}"),
        StreamChunk(type="tool_use_end", tool_id=tool_id),
        StreamChunk(type="done", stop_reason="tool_use", usage={"input_tokens": 10, "output_tokens": 5}),
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
        self.persisted: list[str] = []

    async def set_processing(self) -> None:
        pass

    async def persist_turn(self, user_text, assistant_summary, tool_calls=None, model=None):
        self.persisted.append(user_text)

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

    def __init__(self, tool_result: ToolResult | None = None) -> None:
        super().__init__(
            name="test",
            tools=[ToolDefinition(name="build", description="d")],
            context_builder=_Context(),
        )
        self._tool_result = tool_result or ToolResult(success=True, summary="built")

    async def _execute_tool(self, *a, **k):
        return self._tool_result

    async def build_dynamic_context(self, session) -> str:
        return ""

    async def _observe_to_lore(self, *a, **k):
        pass

    async def _on_loop_complete(self, *a, **k):
        pass

    async def _lore_turn_context(self, session) -> str:
        return ""


def _steer_events(stream: AgentEventStream) -> list[dict]:
    events = []
    while not stream._queue.empty():
        item = stream._queue.get_nowait()
        if getattr(item, "event", None) == AgentEventType.STEER:
            events.append(item.data)
    return events


class SteerInLoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import app.core.agent as agent_module
        from app.services import billing

        self._agent_module = agent_module
        self._saved = (agent_module.get_llm_provider, billing.check_serving_status, billing.charge_llm_call)
        self._billing = billing

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

    async def test_steer_reopens_a_finished_turn(self):
        """The model said its piece; the steer runs the loop again rather than
        waiting for the next message. This is steering the ANSWER."""
        provider = ScriptedProvider([
            _text_turn("Building the login page."),
            _text_turn("Switching to the signup page."),
        ])
        self._install(provider)
        agent, session, stream = _Agent(), _Session(), AgentEventStream()
        # Typed while the model was writing its answer.
        provider.on_call = lambda n: stream.push_steer("no, the signup page", "s1") if n == 1 else None

        await agent._run_loop("build the login page", session, stream)

        self.assertEqual(len(provider.calls), 2, "the steer did not re-open the turn")
        # The model was shown it as a user message of its own: the tail before
        # it was the assistant's finished answer.
        second = provider.calls[1]
        self.assertEqual(second[-1]["role"], "user")
        self.assertIn("no, the signup page", _flatten(second[-1]["content"]))
        # Acknowledged, once, as applied.
        self.assertEqual(_steer_events(stream), [
            {"id": "s1", "text": "no, the signup page", "applied": True},
        ])
        # And the saved turn records both halves of what was asked.
        self.assertEqual(session.persisted, ["build the login page\n\nno, the signup page"])

    async def test_steer_during_a_tool_rides_the_tool_result_message(self):
        """Arriving mid-tool, it joins the tool_result message. Two consecutive
        user messages would be rejected by the provider."""
        provider = ScriptedProvider([_tool_turn("build"), _text_turn("Done, in blue.")])
        self._install(provider)
        agent, session, stream = _Agent(), _Session(), AgentEventStream()
        # Typed while the tool was running: queued after call 1 was issued.
        provider.on_call = lambda n: stream.push_steer("make it blue", "s2") if n == 1 else None

        await agent._run_loop("build the login page", session, stream)

        self.assertEqual(len(provider.calls), 2)
        tail = provider.calls[1][-1]
        self.assertEqual(tail["role"], "user")
        self.assertEqual(tail["content"][0]["type"], "tool_result")
        self.assertIn("make it blue", _flatten(tail["content"]))
        # No consecutive user messages anywhere in what the model was shown.
        roles = [m["role"] for m in provider.calls[1]]
        self.assertFalse(
            any(a == b == "user" for a, b in zip(roles, roles[1:])),
            f"consecutive user messages: {roles}",
        )
        self.assertEqual(_steer_events(stream), [
            {"id": "s2", "text": "make it blue", "applied": True},
        ])

    async def test_several_steers_become_one_injection_but_keep_their_bubbles(self):
        provider = ScriptedProvider([_text_turn("Building."), _text_turn("Revised.")])
        self._install(provider)
        agent, session, stream = _Agent(), _Session(), AgentEventStream()

        def _two(n):
            if n != 1:
                return
            stream.push_steer("make it blue", "a")
            stream.push_steer("and wider", "b")

        provider.on_call = _two

        await agent._run_loop("build it", session, stream)

        tail = provider.calls[1][-1]
        # One text block carrying both, not one block each: a fast typist must
        # not be able to stack blocks onto the message.
        blocks = [b for b in tail["content"] if b.get("type") == "text"]
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["text"], "make it blue\n\nand wider")
        self.assertEqual([e["id"] for e in _steer_events(stream)], ["a", "b"])

    async def test_stopped_run_reports_the_steer_unapplied(self):
        """Stop wins. The text never reached the model, so it is handed back
        rather than acknowledged."""
        provider = ScriptedProvider([_text_turn("Building.")])
        self._install(provider)
        agent, session, stream = _Agent(), _Session(), AgentEventStream()

        def _mid_call(n):
            stream.push_steer("make it blue", "s3")
            stream.cancel()

        provider.on_call = _mid_call

        await agent._run_loop("build it", session, stream)

        self.assertEqual(len(provider.calls), 1, "a cancelled run must not take another turn")
        self.assertEqual(_steer_events(stream), [
            {"id": "s3", "text": "make it blue", "applied": False},
        ])
        # Nothing was folded in, so the saved turn is what the user opened with.
        self.assertEqual(session.persisted, ["build it"])

    async def test_no_steer_changes_nothing(self):
        provider = ScriptedProvider([_text_turn("Building.")])
        self._install(provider)
        agent, session, stream = _Agent(), _Session(), AgentEventStream()

        await agent._run_loop("build it", session, stream)

        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(_steer_events(stream), [])
        self.assertEqual(session.persisted, ["build it"])


def _flatten(content) -> str:
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content if isinstance(b, dict))


if __name__ == "__main__":
    unittest.main()
