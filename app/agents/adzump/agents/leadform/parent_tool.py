"""Orchestrator tool wrapper to trigger the Lead Form Sub-Agent."""

import logging

from app.core.agent import ToolDefinition, ToolResult
from app.core.tools.base import ToolParameter

from app.agents.adzump.tools.campaign_data import _last_user_text

logger = logging.getLogger(__name__)


async def _suggest_lead_form(params: dict, context: dict) -> ToolResult:
    """Main entry point called by the Adzump orchestrator."""
    session = context.get("_session")
    raw_user_message = _last_user_text({"_session": session}) if session else ""
    user_message = raw_user_message or (params.get("user_message") or "").strip()
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

    spec = parent_ctx.get("campaign_spec", {})
    if spec.get("platform") and spec["platform"].lower() != "meta":
        return ToolResult(
            success=False,
            error="Lead forms are only supported for Meta campaigns.",
        )
    if not spec.get("fb_page"):
        return ToolResult(
            success=False,
            error=(
                "A Facebook Page must be selected before creating a lead form. "
                "Ask the user to select one first."
            ),
        )

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
        data={"elicited": True},
        summary="The lead form agent replied directly to the user. Do not restate what it did."
    )


SUGGEST_LEAD_FORM = ToolDefinition(
    name="suggest_lead_form",
    description="Triggers the specialized Lead Form Agent to create or edit a Meta Instant Form.",
    parameters=[
        ToolParameter(
            name="user_message",
            type="string",
            description=(
                "The user's exact, verbatim message from the current turn. "
                "CRITICAL: Pass the user's raw message as-is. NEVER rephrase, "
                "summarize, expand, or combine with previous instructions."
            ),
        )
    ],
    execute=_suggest_lead_form
)
