"""SummaryAgent — single-shot BaseAgent for the gpt-4o profile summary.

Replaces the direct ``openai.chat.completions.create(...)`` call in
``agents/product/tools/scrape/profile.py``. The agent runs ``BaseAgent.run()``
with no tools and ``max_turns=1`` — a single LLM call wrapped in the
standard agent loop so it picks up the same observability, retry, and
event-streaming hooks as ProductAgent.

Why an agent and not a direct call: Adzump's convention is "every LLM
work unit is a BaseAgent subclass" (see
``feedback_traced_llm_not_agent.md`` in the workspace memory). The
~20 ms of loop ceremony is negligible against a ~3 s gpt-4o response,
and consistency beats abstraction-purity for the team's shape.

Parallel-call shape is preserved: callers spawn this via
``asyncio.create_task(get_summary_agent().summarize(...))`` exactly as
they did the old direct-call closure.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.agent import BaseAgent
from app.core.session import BaseSession, AuthContext
from app.core.streaming import AgentEventStream

from app.agents.adzump.agents.summary.context import build_summary_context
from app.agents.adzump.agents.summary.models import SummaryInput, SummaryOutput

logger = logging.getLogger(__name__)


# ── Configuration ─────────────────────────────────────────────────────
#
# Sticking with gpt-4o for the summary call. ProductAgent uses Sonnet
# 4.6 (Anthropic) for orchestration; this is the helper LLM. Different
# provider is fine — BaseAgent's ``provider`` arg routes correctly.
SUMMARY_PROVIDER = "openai"
SUMMARY_MODEL_TIER = "balanced"
SUMMARY_MODEL_OVERRIDE = "openai:gpt-4o"

# The old direct call set ``max_tokens=3000``. Keeping the same ceiling.
SUMMARY_MAX_TOKENS = 3000

# Single-shot: one LLM call, no tools, no second turn.
SUMMARY_MAX_TURNS = 1


class _CraftBoundStream(AgentEventStream):
    """Routes assistant text deltas to a specific craft-panel block.

    The default BaseAgent loop calls ``event_stream.emit_text(delta)`` —
    that lands in the chat surface. The summary should land in the
    ``summary_text`` craft block instead, so this wrapper rewrites
    ``emit_text → emit_craft_text(craft_id, ...)``. Other event types
    pass through to the parent.

    DRAFT-NOTE: this mirrors ``_PassthroughEventStream`` in ProductAgent
    but with a different text-routing target. If we end up with three
    or more such streams, factor a base class.
    """

    def __init__(self, parent: AgentEventStream, craft_id: str) -> None:
        # Deliberately do NOT call super().__init__(): we only delegate.
        self._parent = parent
        self._craft_id = craft_id

    @property
    def is_cancelled(self) -> bool:
        return getattr(self._parent, "is_cancelled", False)

    def cancel(self) -> None:
        try:
            self._parent.cancel()
        except Exception:
            pass

    async def emit_text(self, text: str) -> None:
        # Route streamed summary tokens to the craft block.
        try:
            await self._parent.emit_craft_text(self._craft_id, text)
        except Exception:
            pass

    async def emit_thinking(self, reasoning: str) -> None:
        # Summary agent doesn't surface reasoning to the user.
        return

    async def emit_tool_start(self, *a, **kw) -> None:
        return  # No tools.

    async def emit_tool_update(self, *a, **kw) -> None:
        return

    async def emit_tool_result(self, *a, **kw) -> None:
        return

    async def emit_error(self, message: str) -> None:
        logger.debug("summary_substream_error: %s", message[:200])

    async def emit_done(self, *a, **kw) -> None:
        return  # Parent owns the done event.

    async def emit_keepalive(self) -> None:
        return

    async def emit_suggestions(self, options, mode="single") -> None:
        return

    async def emit_data(self, data_type: str, payload: dict) -> None:
        await self._parent.emit_data(data_type, payload)

    async def emit_agent_started(self, agent_id, label, parent_id="root",
                                 parent_tool_use_id="",
                                 agent_tool_use_id="") -> None:
        await self._parent.emit_agent_started(
            agent_id, label, parent_id, parent_tool_use_id,
            agent_tool_use_id=agent_tool_use_id,
        )

    async def emit_agent_finished(self, agent_id, status="success",
                                  duration_ms=0, tokens_in=0, tokens_out=0,
                                  step_count=0, summary="") -> None:
        await self._parent.emit_agent_finished(
            agent_id, status, duration_ms, tokens_in, tokens_out,
            step_count, summary,
        )

    async def emit_agent_usage(self, agent_id, tokens_in, tokens_out) -> None:
        await self._parent.emit_agent_usage(agent_id, tokens_in, tokens_out)

    async def emit_craft(self, craft_id, title, blocks, message_id="", append=False) -> None:
        await self._parent.emit_craft(craft_id, title, blocks, message_id=message_id, append=append)

    async def emit_craft_text(self, craft_id: str, text_delta: str) -> None:
        await self._parent.emit_craft_text(craft_id, text_delta)

    async def emit_feedback_request(self, session_id, turn_number) -> None:
        return


class SummaryAgent(BaseAgent):
    """Single-shot agent that turns a scraped page into a product summary."""

    display_name = "Profile Writer"

    _instance: "SummaryAgent | None" = None

    def __init__(self) -> None:
        context = build_summary_context()
        # Skip the async doc-load phase — there are no doc paths.
        context._cached_static_text = context._static_prefix

        super().__init__(
            name="summary_gen",
            tools=[],
            context_builder=context,
            model_tier=SUMMARY_MODEL_TIER,
            max_turns=SUMMARY_MAX_TURNS,
            max_tokens=SUMMARY_MAX_TOKENS,
            provider=SUMMARY_PROVIDER,
            sequential_tools=False,
            context_management=None,
        )

    @classmethod
    def get_instance(cls) -> "SummaryAgent":
        if cls._instance is None:
            cls._instance = cls()
            logger.info("SummaryAgent created (single-shot, no tools)")
        return cls._instance

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        # Nothing to expose to tools — there aren't any. Keep the override
        # for symmetry with ProductAgent / future agents.
        return super().build_tool_context(session)

    async def summarize(
        self,
        scraped_text: str,
        url: str,
        parent_event_stream: AgentEventStream,
        parent_tool_use_id: str,
        auth: AuthContext,
        craft_id: str,
        parent_session_context: dict | None = None,
        agent_tool_use_id: str = "",
    ) -> SummaryOutput:
        """Run one summary pass and return the accumulated text.

        Streams text deltas into the craft panel's ``summary_text`` block
        via ``_CraftBoundStream``. Parent stream owns all other events.
        """
        sub_session = BaseSession(agent_name="summary_gen")
        await sub_session.get_or_create(None, auth)

        # Selective context sharing — the sub-agent doesn't need much,
        # but pass craft_id along so any future builder hooks can use it.
        if parent_session_context is not None:
            sub_session.context = {
                "craft_id": parent_session_context.get("craft_id", craft_id),
                "url": url,
            }

        wrapped_stream = _CraftBoundStream(parent_event_stream, craft_id)

        # The system prompt already names the task ("summarise this
        # business"); the user message is just the scraped content.
        # Truncate at 15k chars — same cap as the old direct call.
        msg = f"Website: {url}\n\n{(scraped_text or '')[:15000]}"

        await self.run(
            user_message=msg,
            session=sub_session,
            event_stream=wrapped_stream,
            model_override=SUMMARY_MODEL_OVERRIDE,
            parent_tool_use_id=parent_tool_use_id,
            agent_tool_use_id=agent_tool_use_id,
        )

        # Find the last assistant message — that's the summary text.
        final_text = ""
        for m in reversed(sub_session.get_messages()):
            if m.get("role") != "assistant":
                continue
            content = m.get("content")
            if isinstance(content, str):
                final_text = content
                break
            if isinstance(content, list):
                parts = [c.get("text", "") for c in content if c.get("type") == "text"]
                if any(parts):
                    final_text = "\n".join(p for p in parts if p)
                    break

        return SummaryOutput(text=final_text.strip())


def get_summary_agent() -> SummaryAgent:
    """Module-level accessor for the shared SummaryAgent singleton."""
    return SummaryAgent.get_instance()
