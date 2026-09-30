"""Audience question / edit routing for Adzump.

The orchestrator routes audience requests here; the AudienceAgent's own agentic loop
(handle -> lookup/search/edit tools) decides what to answer or change.
"""

from __future__ import annotations

import logging

from app.agents.adzump.agents.campaign.google.audience.agent import (
    get_audience_manage_agent,
)
from app.agents.adzump.agents.campaign.models import audience
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


async def _manage_audience(params: dict, context: dict) -> ToolResult:
    """Orchestrator-facing entry point for audience questions and edits.

    The orchestrator (Adzump) is a pure router - it does NOT answer audience questions or
    decide which edit to make. It routes "this is about who the campaign reaches" here with
    the user's verbatim message.

    The audience agent holds the reasons and the only catalogue that can resolve a segment;
    answering here would mean inventing both.
    """
    user_message = (params.get("user_message") or "").strip()
    if not user_message:
        return ToolResult(
            success=False,
            error=(
                "manage_audience requires a `user_message` - the orchestrator should "
                "forward the user's verbatim text. Empty messages are rejected here so "
                "the orchestrator gets a clean retry signal."
            ),
        )
    # The manage prompt is built from the saved audience; without one it has nothing to
    # answer from and the orchestrator would fall through to rebuilding the campaign.
    session_ctx = context.get("session_context") or {}
    if audience(session_ctx) is None:
        logger.warning("manage_audience: no audience built yet")
        return ToolResult(
            success=False,
            error=(
                "This campaign has no audience yet - only a Demand Gen campaign has one, "
                "and it must be built first. Do NOT rebuild the campaign to get one."
            ),
        )
    return await get_audience_manage_agent().handle(user_message, context)


manage_audience = ToolDefinition(
    name="manage_audience",
    description=(
        "Handle anything the user says about who the campaign reaches - a question "
        '("why are we targeting X?", "who does this actually reach?") or a change '
        '("add something for young families", "drop the finance ones", "only target '
        '25-44"). Always pass their verbatim message via `user_message`; do NOT answer '
        "audience questions yourself and do NOT classify the request - the audience agent "
        "recorded why each segment was chosen and holds Google's catalogue, and it decides "
        "what to do. Only route here once an audience exists."
    ),
    display_name="Audience",
    parameters=[
        ToolParameter(
            name="user_message",
            type="string",
            description=(
                "The user's verbatim message about the audience. Examples: 'why are we "
                "targeting apartment buyers?', 'who does this reach?', 'add something for "
                "new parents', 'drop the luxury one', 'target only 25 to 44'."
            ),
            required=True,
        ),
        # NOTE: which segment, add-vs-remove and the demographic values are deliberately NOT
        # exposed. They belong to the audience agent's own loop, which extracts them from the
        # user's message. Exposing them here would let the orchestrator fabricate structured
        # edits with no catalogue check (same rule as manage_keywords).
    ],
    execute=_manage_audience,
)

AUDIENCE_TOOLS = [manage_audience]
