"""CreativeGeneratorAgent — orchestrator for ad creative generation and modification.

Follows the adzump sub-agent pattern (singleton, wrapped event stream,
tool_context exposure) but does NOT extend BaseAgent — the creative
pipeline is a sequential workflow, not a tool-use loop.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.adzump.agents.creative_generator.generator import (
    CreativeGenerationService,
)
from app.agents.adzump.agents.creative_generator.context import build_creative_context
from app.core.streaming import AgentEventStream

logger = logging.getLogger(__name__)


class _PassthroughEventStream(AgentEventStream):
    """Event stream wrapper for the creative generator sub-agent.

    Forwards user-visible progress (emit_progress → emit_tool_update) to
    the parent stream and drops everything else — the parent agent owns
    those events.
    """

    def __init__(self, parent: AgentEventStream, parent_tool_use_id: str) -> None:
        self._parent = parent
        self._parent_tool_use_id = parent_tool_use_id

    @property
    def is_cancelled(self) -> bool:
        return getattr(self._parent, "is_cancelled", False)

    def cancel(self) -> None:
        try:
            self._parent.cancel()
        except Exception:
            pass

    async def emit_text(self, text: str) -> None:
        return

    async def emit_thinking(self, reasoning: str) -> None:
        pass

    async def emit_tool_start(self, tool_name, tool_input, tool_use_id="", display_name="") -> None:
        pass

    async def emit_tool_update(self, tool_use_id: str, message: str) -> None:
        await self._parent.emit_tool_update(tool_use_id, message)

    async def emit_tool_result(self, tool_name, success, summary, tool_use_id="") -> None:
        pass

    async def emit_error(self, message: str) -> None:
        logger.debug("creative_substream_error: %s", message[:200])

    async def emit_done(self, session_id: str = "", usage: dict | None = None) -> None:
        return

    async def emit_keepalive(self) -> None:
        return

    async def emit_suggestions(self, options, mode="single") -> None:
        return

    async def emit_data(self, data_type: str, payload: dict) -> None:
        await self._parent.emit_data(data_type, payload)

    async def emit_agent_started(self, agent_id: str, label: str, parent_id: str = "root",
                                 parent_tool_use_id: str = "",
                                 agent_tool_use_id: str = "") -> None:
        await self._parent.emit_agent_started(
            agent_id, label, parent_id, parent_tool_use_id,
            agent_tool_use_id=agent_tool_use_id,
        )

    async def emit_agent_finished(self, agent_id: str, status: str = "success",
                                  duration_ms: int = 0, tokens_in: int = 0, tokens_out: int = 0,
                                  step_count: int = 0, summary: str = "") -> None:
        await self._parent.emit_agent_finished(
            agent_id, status, duration_ms, tokens_in, tokens_out, step_count, summary,
        )

    async def emit_agent_usage(self, agent_id: str, tokens_in: int, tokens_out: int) -> None:
        await self._parent.emit_agent_usage(agent_id, tokens_in, tokens_out)

    async def emit_craft(self, craft_id, title, blocks, message_id="", append=False) -> None:
        await self._parent.emit_craft(craft_id, title, blocks, message_id=message_id, append=append)

    async def emit_craft_text(self, craft_id: str, text_delta: str) -> None:
        await self._parent.emit_craft_text(craft_id, text_delta)

    async def emit_feedback_request(self, session_id: str, turn_number: int) -> None:
        return


class CreativeGeneratorAgent:
    """Orchestrator for ad creative generation and modification pipelines.

    Singleton. Delegates to the pipeline modules (fresh_generation.py,
    modification.py) via CreativeGenerationService.
    """

    display_name = "Creative Generator"

    _instance: CreativeGeneratorAgent | None = None

    def __init__(self) -> None:
        self._system_prompt = build_creative_context()

    @classmethod
    def get_instance(cls) -> CreativeGeneratorAgent:
        if cls._instance is None:
            cls._instance = cls()
            logger.info("CreativeGeneratorAgent created")
        return cls._instance

    async def generate(self, params: dict, context: dict) -> Any:
        """Generate fresh ad copy and square creatives from scratch."""
        wrapped_stream = _PassthroughEventStream(
            context.get("event_stream"),
            context.get("tool_use_id", ""),
        )
        context["event_stream"] = wrapped_stream
        service = CreativeGenerationService(context)
        return await service.generate_fresh_creatives(params)

    async def modify(self, params: dict, context: dict) -> Any:
        """Modify, update, or regenerate formats for a specific existing creative."""
        wrapped_stream = _PassthroughEventStream(
            context.get("event_stream"),
            context.get("tool_use_id", ""),
        )
        context["event_stream"] = wrapped_stream
        service = CreativeGenerationService(context)
        return await service.modify_existing_creative(params)


def get_creative_generator_agent() -> CreativeGeneratorAgent:
    """Module-level accessor for the shared creative generator singleton."""
    return CreativeGeneratorAgent.get_instance()
