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
from dataclasses import dataclass, field
from typing import Any

import asyncio
import json
import logging
from contextvars import ContextVar
from enum import Enum

logger = logging.getLogger(__name__)


# Identifies which agent is currently executing. Set by BaseAgent.run() and
# read by the emit_* methods to tag every event with its originating agent.
# Default "root" = top-level chat agent (no card rendered for it).
current_agent_id: ContextVar[str] = ContextVar("current_agent_id", default="root")


class AgentEventType(str, Enum):
    """Types of events emitted during agent execution."""

    TEXT = "text"  # Streamed text from the LLM
    THINKING = "thinking"  # CoT reasoning from thinking-mode providers
    TOOL_START = "tool_start"  # Tool execution started
    TOOL_UPDATE = "tool_update"  # Progress update during tool execution
    TOOL_RESULT = "tool_result"  # Tool execution completed
    ERROR = "error"  # Error occurred
    DONE = "done"  # Agent finished
    KEEPALIVE = "keepalive"  # Connection keepalive ping
    FEEDBACK_REQUEST = "feedback_request"  # Ask client to show feedback UI
    SUGGESTIONS = "suggestions"  # Clickable option buttons for the user
    CRAFT = "craft"  # Rich structured content for the side panel
    DATA = "data"  # Generic agent-defined inline payload (widget type + fields)
    COMPLETE = "complete"  # Successful terminal state with a structured result payload
    AGENT_STARTED = "agent_started"  # Sub-agent started
    AGENT_FINISHED = "agent_finished"  # Sub-agent finished
    AGENT_USAGE = "agent_usage"  # Token usage update for an agent
    CONFIRMATION_REQUEST = (
        "confirmation_request"  # Ask user to approve/choose before tool execution
    )


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
                future.set_result(
                    {"approved": False, "selected": "deny", "reason": "cancelled"}
                )

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    # ── Emit methods (producer side) ────────────────────────────

    async def emit_text(self, text: str) -> None:
        """Emit a text chunk from the LLM response."""
        await self._queue.put(
            AgentEvent(
                event=AgentEventType.TEXT,
                data={"text": text, "agent_id": current_agent_id.get()},
            )
        )

    async def emit_thinking(self, reasoning: str) -> None:
        """Emit CoT reasoning from a thinking-mode provider (e.g. DeepSeek)."""
        await self._queue.put(
            AgentEvent(
                event=AgentEventType.THINKING,
                data={"text": reasoning, "agent_id": current_agent_id.get()},
            )
        )

    async def emit_tool_start(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_use_id: str = "",
        display_name: str = "",
    ) -> None:
        """Emit when a tool call begins."""
        await self._queue.put(
            AgentEvent(
                event=AgentEventType.TOOL_START,
                data={
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "tool_use_id": tool_use_id,
                    "display_name": display_name,
                    "agent_id": current_agent_id.get(),
                },
            )
        )

    async def emit_tool_update(self, tool_use_id: str, message: str) -> None:
        """Emit a progress message for a running tool."""
        await self._queue.put(
            AgentEvent(
                event=AgentEventType.TOOL_UPDATE,
                data={
                    "tool_use_id": tool_use_id,
                    "message": message,
                    "agent_id": current_agent_id.get(),
                },
            )
        )

    async def emit_tool_result(
        self, tool_name: str, success: bool, summary: str, tool_use_id: str = ""
    ) -> None:
        """Emit when a tool call completes."""
        await self._queue.put(
            AgentEvent(
                event=AgentEventType.TOOL_RESULT,
                data={
                    "tool_name": tool_name,
                    "success": success,
                    "summary": summary,
                    "tool_use_id": tool_use_id,
                    "agent_id": current_agent_id.get(),
                },
            )
        )

    # ── Sub-agent lifecycle events ──────────────────────────────

    async def emit_agent_started(
        self,
        agent_id: str,
        label: str,
        parent_id: str = "root",
        parent_tool_use_id: str = "",
    ) -> None:
        """Emit when a sub-agent begins execution.

        Frontend uses this to render an AgentGroup row for all subsequent
        events tagged with this agent_id, and to suppress the spawning
        tool call (`parent_tool_use_id`) from the "Used N tools" list.
        """
        await self._queue.put(
            AgentEvent(
                event=AgentEventType.AGENT_STARTED,
                data={
                    "agent_id": agent_id,
                    "label": label,
                    "parent_id": parent_id,
                    "parent_tool_use_id": parent_tool_use_id,
                },
            )
        )

    async def emit_agent_finished(
        self,
        agent_id: str,
        status: str = "success",
        duration_ms: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        step_count: int = 0,
        summary: str = "",
    ) -> None:
        """Emit when a sub-agent finishes execution.

        `summary` is a one-line outcome shown in the collapsed card header.
        `step_count` is the number of tool calls / inner items.
        """
        await self._queue.put(
            AgentEvent(
                event=AgentEventType.AGENT_FINISHED,
                data={
                    "agent_id": agent_id,
                    "status": status,
                    "duration_ms": duration_ms,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "step_count": step_count,
                    "summary": summary,
                },
            )
        )

    async def emit_agent_usage(
        self,
        agent_id: str,
        tokens_in: int,
        tokens_out: int,
    ) -> None:
        """Emit an interim token usage update for an agent."""
        await self._queue.put(
            AgentEvent(
                event=AgentEventType.AGENT_USAGE,
                data={
                    "agent_id": agent_id,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                },
            )
        )

    async def emit_error(self, message: str) -> None:
        """Emit an error event."""
        await self._queue.put(
            AgentEvent(
                event=AgentEventType.ERROR,
                data={"message": message},
            )
        )

    async def emit_done(
        self, session_id: str = "", usage: dict[str, Any] | None = None
    ) -> None:
        """Emit done event and close the stream."""
        await self._queue.put(
            AgentEvent(
                event=AgentEventType.DONE,
                data={
                    "session_id": session_id,
                    "usage": usage or {},
                },
            )
        )
        # Signal end of stream
        await self._queue.put(_SENTINEL)

    async def emit_keepalive(self) -> None:
        """Emit a keepalive ping to prevent connection timeout."""
        await self._queue.put(
            AgentEvent(
                event=AgentEventType.KEEPALIVE,
                data={},
            )
        )

    async def emit_suggestions(
        self,
        options: list[dict[str, str]],
        mode: str = "single",
    ) -> None:
        """Emit clickable suggestion options for the UI.

        Args:
            options: List of {label, value} dicts.
            mode: "single" (click sends immediately) or "multi" (toggle + confirm).
        """
        await self._queue.put(
            AgentEvent(
                event=AgentEventType.SUGGESTIONS,
                data={"options": options, "mode": mode},
            )
        )

    async def emit_data(self, data_type: str, payload: dict[str, Any]) -> None:
        """Emit an agent-defined inline payload.

        The UI dispatches on `type` and renders a matching widget. Keeps the
        core domain-agnostic — each agent picks its own widget types.
        """
        await self._queue.put(
            AgentEvent(
                event=AgentEventType.DATA,
                data={"type": data_type, **payload},
            )
        )

    async def emit_complete(self, payload: dict[str, Any]) -> None:
        """Emit a successful terminal-state event with a structured payload.

        Distinct from `done`, which is the always-fired stream lifecycle
        terminator (carries session_id + usage). `complete` is the
        domain-task signal: fired only when an agent reaches a successful
        terminal state with a result the host page should act on (e.g.
        redirect, persist, call a downstream API).

        Pairs with the LazyPrompt component's `onComplete` /
        `completeBindingPath` props.
        """
        await self._queue.put(
            AgentEvent(
                event=AgentEventType.COMPLETE,
                data=payload,
            )
        )

    async def emit_craft(
        self,
        craft_id: str,
        title: str,
        blocks: list[dict[str, Any]],
        message_id: str = "",
        append: bool = False,
    ) -> None:
        """Emit rich structured content for the side panel.

        Args:
            craft_id: Unique identifier for this craft (same id = update).
            title: Display title for the panel header and inline card.
            blocks: Array of block objects (heading, text, table, etc.).
            message_id: Links this craft to a specific chat message.
            append: If True, blocks are appended to existing craft. If False, replaces.
        """
        data: dict[str, Any] = {
            "id": craft_id,
            "title": title,
            "blocks": blocks,
            "message_id": message_id,
        }
        if append:
            data["append"] = True
        await self._queue.put(
            AgentEvent(
                event=AgentEventType.CRAFT,
                data=data,
            )
        )

    async def emit_craft_text(self, craft_id: str, text_delta: str) -> None:
        """Append text to the craft panel's text block."""
        await self._queue.put(
            AgentEvent(
                event=AgentEventType.CRAFT,
                data={"id": craft_id, "text_delta": text_delta},
            )
        )

    async def emit_feedback_request(self, session_id: str, turn_number: int) -> None:
        """Emit an event asking the client to show the feedback UI."""
        await self._queue.put(
            AgentEvent(
                event=AgentEventType.FEEDBACK_REQUEST,
                data={"session_id": session_id, "turn_number": turn_number},
            )
        )

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

        await self._queue.put(
            AgentEvent(
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
            )
        )

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return {"approved": False, "selected": "deny", "reason": "timeout"}
        finally:
            self._pending_confirmations.pop(confirmation_id, None)

    def resolve_confirmation(
        self, confirmation_id: str, response: dict[str, Any]
    ) -> bool:
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


class PassthroughEventStream(AgentEventStream):
    """Sub-agent event stream that forwards progress events to a parent stream.

    Forwards user-visible progress (craft, tool lifecycle, agent lifecycle) to the
    parent stream rewritten under the parent's tool_use_id, and drops everything
    else (text, done, error) — the parent agent owns those.

    Used by sub-agents (optimization analyst, product analyst, etc.) that run
    inside a parent tool call and should not emit their own text stream.
    """

    def __init__(self, parent: "AgentEventStream", parent_tool_use_id: str) -> None:
        # Deliberately do NOT call super().__init__(): we don't want a local
        # queue; this wrapper only delegates to the parent stream.
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

    async def emit_tool_start(
        self, tool_name, tool_input, tool_use_id="", display_name=""
    ) -> None:
        await self._parent.emit_tool_start(
            tool_name, tool_input, tool_use_id, display_name
        )

    async def emit_tool_update(self, tool_use_id: str, message: str) -> None:
        await self._parent.emit_tool_update(tool_use_id, message)

    async def emit_tool_result(
        self, tool_name, success, summary, tool_use_id=""
    ) -> None:
        await self._parent.emit_tool_result(tool_name, success, summary, tool_use_id)

    async def emit_error(self, message: str) -> None:
        logger.debug("passthrough_substream_error: %s", message[:200])

    async def emit_done(self, session_id: str = "", usage: dict | None = None) -> None:
        return

    async def emit_keepalive(self) -> None:
        return

    async def emit_suggestions(self, options, mode: str = "single") -> None:
        return

    async def emit_data(self, data_type: str, payload: dict) -> None:
        await self._parent.emit_data(data_type, payload)

    async def emit_agent_started(
        self,
        agent_id: str,
        label: str,
        parent_id: str = "root",
        parent_tool_use_id: str = "",
    ) -> None:
        await self._parent.emit_agent_started(
            agent_id, label, parent_id, parent_tool_use_id
        )

    async def emit_agent_finished(
        self,
        agent_id: str,
        status: str = "success",
        duration_ms: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        step_count: int = 0,
        summary: str = "",
    ) -> None:
        await self._parent.emit_agent_finished(
            agent_id,
            status,
            duration_ms,
            tokens_in,
            tokens_out,
            step_count,
            summary,
        )

    async def emit_agent_usage(
        self, agent_id: str, tokens_in: int, tokens_out: int
    ) -> None:
        await self._parent.emit_agent_usage(agent_id, tokens_in, tokens_out)

    async def emit_craft(
        self,
        craft_id: str,
        title: str,
        blocks: list,
        message_id: str = "",
        append: bool = False,
    ) -> None:
        await self._parent.emit_craft(
            craft_id, title, blocks, message_id=message_id, append=append
        )

    async def emit_craft_text(self, craft_id: str, text_delta: str) -> None:
        await self._parent.emit_craft_text(craft_id, text_delta)

    async def request_confirmation(self, *args, **kwargs) -> dict:
        # Delegate to parent — sub-agent confirmations bubble up to the user
        # via the parent stream. Without this, _pending_confirmations and _queue
        # (set by super().__init__, which we skip) would cause AttributeError.
        return await self._parent.request_confirmation(*args, **kwargs)

    def resolve_confirmation(self, confirmation_id: str, response: dict) -> bool:
        return self._parent.resolve_confirmation(confirmation_id, response)

    async def emit_feedback_request(self, session_id: str, turn_number: int) -> None:
        return
