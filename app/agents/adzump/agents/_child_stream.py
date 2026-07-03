"""Shared sub-agent event-stream facade.

A worker sub-agent runs on its parent's SSE connection. This base forwards user-facing
events (panel data/craft, sub-agent lifecycle, confirmations, completion) to the parent
and swallows the agent's internal reasoning plus the terminators the parent owns
(done/feedback/keepalive). Subclasses override only what differs.

No super().__init__(): every base method that touches the queue / confirmation state is
overridden here, so the facade keeps no buffer of its own and nothing falls through to
the parent-less base.
"""

from __future__ import annotations

import logging

from app.core.streaming import AgentEventStream

logger = logging.getLogger(__name__)

_LOG_TRUNCATE = 160


class ChildAgentStream(AgentEventStream):
    """Forwarding facade over a parent stream. Default: forward everything the user
    should see, swallow the agent's reasoning + the parent-owned terminators."""

    label = "sub-agent"  # log prefix for swallowed errors

    def __init__(self, parent: AgentEventStream) -> None:
        self._parent = parent  # no super().__init__() — see module docstring

    # Cancellation — read/drive the parent.
    @property
    def is_cancelled(self) -> bool:
        return getattr(self._parent, "is_cancelled", False)

    def cancel(self) -> None:
        self._parent.cancel()

    # Forwarded — the user should see these.
    async def emit_data(self, *a, **kw):
        await self._parent.emit_data(*a, **kw)

    async def emit_craft(self, *a, **kw):
        await self._parent.emit_craft(*a, **kw)

    async def emit_craft_text(self, *a, **kw):
        await self._parent.emit_craft_text(*a, **kw)

    async def emit_tool_start(self, *a, **kw):
        await self._parent.emit_tool_start(*a, **kw)

    async def emit_tool_update(self, *a, **kw):
        await self._parent.emit_tool_update(*a, **kw)

    async def emit_tool_result(self, *a, **kw):
        await self._parent.emit_tool_result(*a, **kw)

    async def emit_agent_started(self, *a, **kw):
        await self._parent.emit_agent_started(*a, **kw)

    async def emit_agent_finished(self, *a, **kw):
        await self._parent.emit_agent_finished(*a, **kw)

    async def emit_agent_usage(self, *a, **kw):
        await self._parent.emit_agent_usage(*a, **kw)

    async def emit_complete(self, *a, **kw):
        await self._parent.emit_complete(*a, **kw)

    async def request_confirmation(self, *a, **kw):
        return await self._parent.request_confirmation(*a, **kw)

    # Swallowed — internal reasoning + the terminators the parent owns.
    async def emit_text(self, text: str) -> None:
        return

    async def emit_thinking(self, reasoning: str) -> None:
        return

    async def emit_done(self, *a, **kw) -> None:
        return

    async def emit_feedback_request(self, *a, **kw) -> None:
        return

    async def emit_suggestions(self, *a, **kw) -> None:
        return

    async def emit_keepalive(self) -> None:
        return

    async def emit_error(self, message: str) -> None:
        logger.debug("%s stream error: %s", self.label, message[:_LOG_TRUNCATE])
