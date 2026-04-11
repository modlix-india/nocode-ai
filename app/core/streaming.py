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
    THINKING = "thinking"  # CoT reasoning from thinking-mode providers
    TOOL_START = "tool_start"  # Tool execution started
    TOOL_RESULT = "tool_result"  # Tool execution completed
    ERROR = "error"  # Error occurred
    DONE = "done"  # Agent finished
    KEEPALIVE = "keepalive"  # Connection keepalive ping
    FEEDBACK_REQUEST = "feedback_request"  # Ask client to show feedback UI
    CONFIRMATION_REQUEST = "confirmation_request"  # Ask user to approve/choose before tool execution
    PLAN_UPDATE = "plan_update"  # Plan step status changed
    SUBTASK_START = "subtask_start"  # Sub-agent spawned for a task
    SUBTASK_DONE = "subtask_done"  # Sub-agent completed
    PROGRESS = "progress"  # Overall progress update (X of Y steps)


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
        # Pending confirmation requests: confirmation_id → Future[dict]
        self._pending_confirmations: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # Cancellation flag — set by POST /stop, checked by the agent loop
        self._cancelled = False

    # ── Cancellation ─────────────────────────────────────────────

    def cancel(self) -> None:
        """Signal the agent loop to stop at the next checkpoint."""
        self._cancelled = True
        # Unblock any pending confirmation so the loop can exit
        for future in self._pending_confirmations.values():
            if not future.done():
                future.set_result({"approved": False, "selected": "deny", "reason": "cancelled"})

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    # ── Emit methods (producer side) ────────────────────────────

    async def emit_text(self, text: str) -> None:
        """Emit a text chunk from the LLM response."""
        await self._queue.put(AgentEvent(
            event=AgentEventType.TEXT,
            data={"text": text},
        ))

    async def emit_thinking(self, reasoning: str) -> None:
        """Emit CoT reasoning from a thinking-mode provider (e.g. DeepSeek)."""
        await self._queue.put(AgentEvent(
            event=AgentEventType.THINKING,
            data={"text": reasoning},
        ))

    async def emit_tool_start(
        self, tool_name: str, tool_input: dict[str, Any], tool_use_id: str = "", display_name: str = ""
    ) -> None:
        """Emit when a tool call begins."""
        await self._queue.put(AgentEvent(
            event=AgentEventType.TOOL_START,
            data={
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_use_id": tool_use_id,
                "display_name": display_name,
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

    async def emit_feedback_request(self, session_id: str, turn_number: int) -> None:
        """Emit an event asking the client to show the feedback UI."""
        await self._queue.put(AgentEvent(
            event=AgentEventType.FEEDBACK_REQUEST,
            data={"session_id": session_id, "turn_number": turn_number},
        ))

    async def emit_plan_update(
        self, step_id: int, status: str, task: str, notes: str = "",
    ) -> None:
        """Emit a plan step status change."""
        await self._queue.put(AgentEvent(
            event=AgentEventType.PLAN_UPDATE,
            data={"step_id": step_id, "status": status, "task": task, "notes": notes},
        ))

    async def emit_subtask_start(
        self, subtask_id: str, task: str, step_id: int | None = None,
    ) -> None:
        """Emit a sub-agent spawn event."""
        await self._queue.put(AgentEvent(
            event=AgentEventType.SUBTASK_START,
            data={"subtask_id": subtask_id, "task": task, "step_id": step_id},
        ))

    async def emit_subtask_done(
        self, subtask_id: str, success: bool, summary: str, step_id: int | None = None,
    ) -> None:
        """Emit a sub-agent completion event."""
        await self._queue.put(AgentEvent(
            event=AgentEventType.SUBTASK_DONE,
            data={"subtask_id": subtask_id, "success": success, "summary": summary, "step_id": step_id},
        ))

    async def emit_progress(
        self, completed: int, total: int, current_task: str = "",
    ) -> None:
        """Emit an overall progress update."""
        await self._queue.put(AgentEvent(
            event=AgentEventType.PROGRESS,
            data={"completed": completed, "total": total, "current_task": current_task},
        ))

    async def request_confirmation(
        self,
        confirmation_id: str,
        message: str,
        tool_name: str,
        display_name: str,
        details: dict[str, Any] | None = None,
        options: list[dict[str, str]] | None = None,
        timeout: float = 120.0,
        session_id: str = "",
    ) -> dict[str, Any]:
        """Emit a confirmation request and wait for the user's response.

        Blocks the calling coroutine until the user responds via
        resolve_confirmation() or the timeout expires.

        Args:
            confirmation_id: Unique ID for this confirmation.
            message: Human-readable description of what will be changed.
            tool_name: The tool requesting confirmation.
            display_name: Human-friendly tool name.
            details: Optional structured details about the operation.
            options: List of {label, value} dicts. Defaults to Approve/Deny.
            timeout: Seconds to wait before auto-denying.
            session_id: Session ID so the client can POST /confirm before the stream ends.

        Returns:
            Dict with at least {"approved": bool, "selected": str}.
        """
        if options is None:
            options = [
                {"label": "Approve", "value": "approve"},
                {"label": "Deny", "value": "deny"},
            ]

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_confirmations[confirmation_id] = future

        await self._queue.put(AgentEvent(
            event=AgentEventType.CONFIRMATION_REQUEST,
            data={
                "confirmation_id": confirmation_id,
                "message": message,
                "tool_name": tool_name,
                "display_name": display_name,
                "details": details or {},
                "options": options,
                "session_id": session_id,
            },
        ))

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return {"approved": False, "selected": "deny", "reason": "timeout"}
        finally:
            self._pending_confirmations.pop(confirmation_id, None)

    def resolve_confirmation(self, confirmation_id: str, response: dict[str, Any]) -> bool:
        """Resolve a pending confirmation request with the user's response.

        Called by the /confirm endpoint when the user clicks approve/deny.

        Returns:
            True if the confirmation was found and resolved, False otherwise.
        """
        future = self._pending_confirmations.get(confirmation_id)
        if future and not future.done():
            future.set_result(response)
            return True
        return False

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
