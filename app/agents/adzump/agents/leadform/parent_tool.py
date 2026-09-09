"""Orchestrator tool wrapper to trigger the Lead Form Sub-Agent."""

import logging

from app.core.agent import ToolResult, ToolDefinition
from app.core.tools.base import ToolParameter

logger = logging.getLogger(__name__)


async def _suggest_lead_form(params: dict, context: dict) -> ToolResult:
    """Main entry point called by the Adzump orchestrator."""
    user_message = (params.get("user_message") or "").strip()
    if not user_message:
        return ToolResult(
            success=False,
            error=(
                "suggest_lead_form requires a `user_message` - the "
                "orchestrator should forward the user's verbatim text."
            ),
        )

    parent_ctx = context.get("session_context")
    if parent_ctx is None:
        return ToolResult(success=False, error="No session context available.")

    from app.agents.adzump.agents.leadform.agent import run_leadform_session
    
    status = await run_leadform_session(
        user_message=user_message,
        parent_ctx=parent_ctx,
        stream=context.get("event_stream"),
        tool_use_id=context.get("tool_use_id", ""),
        auth_context=context.get("auth")
    )

    if status == "failed":
        return ToolResult(success=False, error="The lead form agent failed to process the request.")

    return ToolResult(
        success=True,
        summary="The lead form agent replied directly to the user. Do not restate what it did."
    )


SUGGEST_LEAD_FORM = ToolDefinition(
    name="suggest_lead_form",
    description="Triggers the specialized Lead Form Agent to create or edit a Meta Instant Form.",
    parameters=[
        ToolParameter(name="user_message", type="string", description="The user's message/instructions.")
    ],
    execute=_suggest_lead_form
)
