"""SSE streaming protocol for agent communication.

Defines event types and an async queue-based event stream that emits
Server-Sent Events to the HTTP response.

SSE wire format per event:
    event: <event_type>
    data: <json_payload>
    \\n

Usage:
    stream = AgentEventStream()

    # Producer (agent loop)
    await stream.emit_text("Hello")
    await stream.emit_tool_start("add_component", {"type": "Button"})
    await stream.emit_tool_result("add_component", True, "Added Button")
    await stream.emit_done(session_id="abc", usage={"input": 100, "output": 50})

    # Consumer (SSE endpoint)
    async for event in stream.events():
        yield event.to_sse()
"""

from __future__ import annotations

import asyncio
import json
from enum import Enum
from dataclasses import dataclass, field
from typing import Any


class AgentEventType(str, Enum):
    """Types of events emitted during agent execution."""

    TEXT = "text"  # Streamed text from the LLM
    TOOL_START = "tool_start"  # Tool execution started
    TOOL_RESULT = "tool_result"  # Tool execution completed
    ERROR = "error"  # Error occurred
    DONE = "done"  # Agent finished
    KEEPALIVE = "keepalive"  # Connection keepalive ping


@dataclass
class AgentEvent:
    """A single SSE event."""

    event: AgentEventType
    data: dict[str, Any] = field(default_factory=dict)

    def to_sse(self) -> str:
        """Format as an SSE string for the HTTP response.

        Returns:
            String in SSE format: "event: <type>\\ndata: <json>\\n\\n"
        """
        payload = json.dumps(self.data, default=str)
        return f"event: {self.event.value}\ndata: {payload}\n\n"


_SENTINEL = object()


class AgentEventStream:
    """Async queue-based event emitter for agent → SSE response.

    The agent loop calls emit_* methods to push events.
    The SSE endpoint consumes via the async events() generator.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[AgentEvent | object] = asyncio.Queue()

    # ── Emit methods (producer side) ────────────────────────────

    async def emit_text(self, text: str) -> None:
        """Emit a text chunk from the LLM response."""
        await self._queue.put(AgentEvent(
            event=AgentEventType.TEXT,
            data={"text": text},
        ))

    async def emit_tool_start(self, tool_name: str, tool_input: dict[str, Any], tool_use_id: str = "") -> None:
        """Emit when a tool call begins."""
        await self._queue.put(AgentEvent(
            event=AgentEventType.TOOL_START,
            data={
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_use_id": tool_use_id,
            },
        ))

    async def emit_tool_result(
        self, tool_name: str, success: bool, summary: str, tool_use_id: str = ""
    ) -> None:
        """Emit when a tool call completes."""
        await self._queue.put(AgentEvent(
            event=AgentEventType.TOOL_RESULT,
            data={
                "tool_name": tool_name,
                "success": success,
                "summary": summary,
                "tool_use_id": tool_use_id,
            },
        ))

    async def emit_error(self, message: str) -> None:
        """Emit an error event."""
        await self._queue.put(AgentEvent(
            event=AgentEventType.ERROR,
            data={"message": message},
        ))

    async def emit_done(self, session_id: str = "", usage: dict[str, Any] | None = None) -> None:
        """Emit done event and close the stream."""
        await self._queue.put(AgentEvent(
            event=AgentEventType.DONE,
            data={
                "session_id": session_id,
                "usage": usage or {},
            },
        ))
        # Signal end of stream
        await self._queue.put(_SENTINEL)

    async def emit_keepalive(self) -> None:
        """Emit a keepalive ping to prevent connection timeout."""
        await self._queue.put(AgentEvent(
            event=AgentEventType.KEEPALIVE,
            data={},
        ))

    # ── Consumer side ───────────────────────────────────────────

    async def events(self):
        """Async generator that yields AgentEvent objects until done.

        Yields:
            AgentEvent objects. Stops after DONE event.
        """
        while True:
            item = await self._queue.get()
            if item is _SENTINEL:
                break
            yield item
