"""Event stream wrapper for DetailedTargetingAgent."""

import logging
from app.core.streaming import AgentEventStream

logger = logging.getLogger(__name__)


class MetaPassthroughEventStream(AgentEventStream):
    """Event stream wrapper used by the sub-agent.

    Forwards user-visible progress (craft, tool_update, agent_*, thinking)
    to the parent stream. Drops text, tool_start, tool_result, done, and
    error — the parent agent owns those channels.
    """

    def __init__(self, parent: AgentEventStream, parent_tool_use_id: str) -> None:
        super().__init__()
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
        await self._parent.emit_thinking(reasoning)

    async def emit_tool_start(self, tool_name, tool_input, tool_use_id="", display_name="") -> None:
        # Drop: inner-loop tool calls are internal to the sub-agent and should
        # not appear as separate tool events in the parent chat stream.
        return

    async def emit_tool_update(self, tool_use_id: str, message: str) -> None:
        target_id = self._parent_tool_use_id or tool_use_id
        await self._parent.emit_tool_update(target_id, message)

    async def emit_tool_result(self, tool_name, success, summary, tool_use_id="") -> None:
        # Drop: inner-loop tool results are internal. The final summary is
        # surfaced via the ToolResult returned by suggest_meta_targeting.
        return

    async def emit_error(self, message: str) -> None:
        logger.debug("analyst_substream_error: %s", message[:200])

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
        target_msg_id = message_id or self._parent_tool_use_id
        await self._parent.emit_craft(
            craft_id, title, blocks, message_id=target_msg_id, append=append
        )
