"""Unit: core BaseAgent run-loop — _with_tail_reminder (replace-not-append,
tail-placed) + audience routing in _run_tool_block (user/both → emit + persist)."""
from __future__ import annotations

import types
import unittest

from app.core.agent import BaseAgent
from app.core.streaming import AgentEventStream, AgentEventType
from app.core.tools.base import ToolDefinition, ToolResult

REMINDER = "State: platform=Google Ads. Next: ask duration."


class WithTailReminderTests(unittest.TestCase):
    def test_empty_reminder_is_passthrough(self):
        msgs = [{"role": "user", "content": "hi"}]
        self.assertIs(BaseAgent._with_tail_reminder(msgs, ""), msgs)

    def test_does_not_mutate_input(self):
        original = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        orig_content = original[-1]["content"]
        orig_blocks = list(orig_content)
        out = BaseAgent._with_tail_reminder(original, REMINDER)
        # input untouched — the no-persist / replace-not-append guarantee
        self.assertEqual(len(original), 1)
        self.assertIs(original[-1]["content"], orig_content)
        self.assertEqual(original[-1]["content"], orig_blocks)
        # output is a NEW list with the reminder appended at the tail
        self.assertIsNot(out, original)
        last = out[-1]["content"]
        self.assertEqual(len(last), 2)
        self.assertEqual(last[-1]["type"], "text")
        self.assertIn("<system-reminder>", last[-1]["text"])
        self.assertIn(REMINDER, last[-1]["text"])

    def test_string_content_becomes_list_reminder_last(self):
        out = BaseAgent._with_tail_reminder([{"role": "user", "content": "what's up"}], REMINDER)
        content = out[-1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "what's up"})
        self.assertIn(REMINDER, content[-1]["text"])

    def test_tool_result_then_reminder(self):
        tr = {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
        out = BaseAgent._with_tail_reminder([{"role": "user", "content": [tr]}], REMINDER)
        content = out[-1]["content"]
        self.assertEqual(content[0], tr)               # tool_result stays first
        self.assertEqual(content[-1]["type"], "text")  # reminder lands last (tail)
        self.assertIn(REMINDER, content[-1]["text"])

    def test_fresh_text_passes_through(self):
        a = BaseAgent._with_tail_reminder([{"role": "user", "content": "x"}], "next: A")
        b = BaseAgent._with_tail_reminder([{"role": "user", "content": "x"}], "next: B")
        self.assertNotEqual(a[-1]["content"][-1]["text"], b[-1]["content"][-1]["text"])

    def test_empty_messages(self):
        out = BaseAgent._with_tail_reminder([], REMINDER)
        self.assertEqual(out[0]["role"], "user")
        self.assertIn(REMINDER, out[0]["content"][0]["text"])


class AudienceRoutingTests(unittest.IsolatedAsyncioTestCase):
    """_run_tool_block posts the summary to chat (emit + persist) iff audience
    targets the user. Replaces relay_summary; NO de-dup — the tool-text contract
    keeps the model to a lead-in, a rare verbatim echo is accepted."""

    async def _run(self, result: ToolResult, streamed: str = ""):
        class _A(BaseAgent):
            async def _execute_tool(self, *a, **k):
                return result
        agent = _A(name="x", tools=[ToolDefinition(name="t", description="d")],
                   context_builder=object())
        stream = AgentEventStream()
        parts: list[str] = []
        session = types.SimpleNamespace(session_id="s", _turn_assistant_text=streamed)
        await agent._run_tool_block({"name": "t", "input": {}, "id": "u1"},
                                    session, stream, parts)
        texts = []
        while not stream._queue.empty():
            ev = stream._queue.get_nowait()
            if getattr(ev, "event", None) == AgentEventType.TEXT:
                texts.append(ev.data["text"])
        return texts, parts

    # Summaries are framed as their own paragraph ("\n\n…\n\n"): the persisted
    # parts are "".join'd stream deltas and the UI concatenates text events, so
    # an unframed summary glues onto prose ("…Pride EuphoraFetched creatives…").
    async def test_user_emits_and_persists_the_summary(self):
        texts, parts = await self._run(ToolResult(success=True, summary="Saved.", audience="user"))
        self.assertEqual(texts, ["\n\nSaved.\n\n"])
        self.assertEqual(parts, ["\n\nSaved.\n\n"])     # persisted → survives refresh

    async def test_both_also_emits(self):
        texts, parts = await self._run(ToolResult(success=True, summary="Found 2.", audience="both"))
        self.assertEqual(texts, ["\n\nFound 2.\n\n"])
        self.assertEqual(parts, ["\n\nFound 2.\n\n"])

    async def test_assistant_default_does_not_post_to_chat(self):
        texts, parts = await self._run(ToolResult(success=True, summary="internal note"))
        self.assertEqual(texts, [])
        self.assertEqual(parts, [])

    async def test_user_with_empty_summary_posts_nothing(self):
        texts, parts = await self._run(ToolResult(success=True, summary="", audience="user"))
        self.assertEqual(texts, [])

    async def test_failed_user_tool_posts_nothing(self):
        texts, _ = await self._run(ToolResult(success=False, error="boom", summary="x", audience="user"))
        self.assertEqual(texts, [])

    async def test_posts_even_when_model_already_echoed_it(self):
        # no de-dup: posts regardless of what the model streamed (both + user).
        for aud in ("both", "user"):
            texts, _ = await self._run(
                ToolResult(success=True, summary="Found 2 competitors: Sobha, Prestige.", audience=aud),
                streamed="Sure — Found 2 competitors: Sobha, Prestige. Continue?")
            self.assertEqual(texts, ["\n\nFound 2 competitors: Sobha, Prestige.\n\n"], aud)


class DeferredElicitationBreakTests(unittest.TestCase):
    """A FAILED elicitation is not an elicitation: the loop must continue so
    the model reads the tool's corrective error and re-calls in the same turn
    (live 2026-09-04: refused present_options + break = silent dead-end loop)."""

    def _entry(self, **overrides) -> dict:
        entry = {"tool": "present_options", "success": True,
                 "kind": "elicitation", "elicit_mode": "deferred",
                 "elicited": False}
        entry.update(overrides)
        return entry

    def test_rows(self):
        rows = [
            ("successful static elicitation breaks", self._entry(), True),
            ("FAILED elicitation never breaks",
             self._entry(success=False), False),
            ("successful runtime signal breaks",
             self._entry(kind="tool", elicited=True), True),
            ("failed runtime signal never breaks",
             self._entry(kind="tool", elicited=True, success=False), False),
            ("blocking elicitation excluded",
             self._entry(elicit_mode="blocking"), False),
            ("plain tool never breaks", self._entry(kind="tool"), False),
        ]
        for label, entry, expected in rows:
            with self.subTest(label):
                self.assertIs(
                    BaseAgent._is_deferred_elicitation(entry), expected)


if __name__ == "__main__":
    unittest.main()
