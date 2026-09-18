"""Sub-agent event stream wrapper for the Lead Form agent.

When the LeadFormAgent runs as a sub-agent (via ``suggest_lead_form``
tool dispatch), it gets its own event stream. We don't want a fresh
queue — we want the sub-agent's events to *forward* to the parent stream
for some event types and *drop* for others.

Forward/drop matrix (mirrors LocationPassthroughEventStream):
  ┌──────────────────────────┬────────────┬────────────────────────────────┐
  │ Event                    │ Behaviour  │ Why                            │
  ├──────────────────────────┼────────────┼────────────────────────────────┤
  │ tool_start / update /    │ forward    │ UI needs to render tool cards  │
  │ result                   │            │                                │
  │ data                     │ forward    │ Widget payloads                │
  │ agent_started / finished │ forward    │ AgentCard lifecycle            │
  │ agent_usage              │ forward    │ Token usage                    │
  │ thinking                 │ forward    │ CoT reasoning                  │
  │ text                     │ forward    │ User sees MANAGE-phase chat    │
  ├──────────────────────────┼────────────┼────────────────────────────────┤
  │ done                     │ drop       │ Parent owns turn-completion    │
  │ keepalive                │ drop       │ Connection-level, parent owns  │
  │ suggestions              │ drop       │ Sub-agent can't ask chips      │
  │ feedback_request         │ drop       │ Sub-agent can't request        │
  │                          │            │ feedback mid-turn              │
  ├──────────────────────────┼────────────┼────────────────────────────────┤
  │ error                    │ log+drop   │ Parent tool wrapper converts   │
  │                          │            │ failures into ToolResult;      │
  │                          │            │ no double-emit.                │
  └──────────────────────────┴────────────┴────────────────────────────────┘

Cancellation is special: ``is_cancelled`` and ``cancel()`` delegate to the
parent so a top-level user-cancel propagates into the sub-agent's run loop
without us having to mirror the cancel flag.
"""

from __future__ import annotations

import logging

from app.core.streaming import AgentEventStream

logger = logging.getLogger(__name__)


class LeadFormEventStream(AgentEventStream):
    """Wraps a parent stream: forwards some events, drops others.

    ``super().__init__()`` is called so base members this class does NOT
    override (emit_complete, request_confirmation, events, ...) find their
    queue instead of crashing on a missing attribute. Nothing consumes the
    local queue — the overrides below decide what actually reaches the user.
    """

    def __init__(self, parent: AgentEventStream) -> None:
        super().__init__()
        self._parent = parent

    # ── Cancellation: delegate ─────────────────────────────────────────────
    @property
    def is_cancelled(self) -> bool:
        return getattr(self._parent, "is_cancelled", False)

    def cancel(self) -> None:
        try:
            self._parent.cancel()
        except Exception:
            pass  # suppress: cancel is best-effort

    # ── Forwarded events (UI needs these) ─────────────────────────────────
    async def emit_text(self, text: str) -> None:
        """Forward chat text — the user sees MANAGE-phase responses."""
        await self._parent.emit_text(text)

    async def emit_thinking(self, reasoning: str) -> None:
        await self._parent.emit_thinking(reasoning)

    async def emit_tool_start(self, tool_name, tool_input, tool_use_id="", display_name="") -> None:
        await self._parent.emit_tool_start(tool_name, tool_input, tool_use_id, display_name)

    async def emit_tool_update(self, tool_use_id: str, message: str) -> None:
        await self._parent.emit_tool_update(tool_use_id, message)

    async def emit_tool_result(self, tool_name, success, summary, tool_use_id="") -> None:
        await self._parent.emit_tool_result(tool_name, success, summary, tool_use_id)

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

    # ── Dropped events (parent owns these) ────────────────────────────────
    async def emit_done(self, session_id: str = "", usage: dict | None = None) -> None:
        return

    async def emit_keepalive(self) -> None:
        return

    async def emit_suggestions(self, options, mode="single") -> None:
        return

    async def emit_feedback_request(self, session_id: str, turn_number: int) -> None:
        return

    # ── Forwarded to parent to ensure UI receives error telemetry ──
    async def emit_error(self, message: str) -> None:
        logger.warning("leadform_substream_error: %s", message[:200])
        await self._parent.emit_error(message)
