from __future__ import annotations

import logging

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


async def _manage_creatives(params: dict, context: dict) -> ToolResult:
    """Route the user's verbatim creative request to the CreativeAgent.

    Pure router — no intent classification here. The CreativeAgent's own
    LLM interprets the request and dispatches internally.
    """
    user_message = (params.get("user_message") or "").strip()
    logger.info("manage_creatives tool invoked with user_message=%r", user_message)
    if not user_message:
        return ToolResult(
            success=False,
            error="manage_creatives requires a `user_message` — forward the user's verbatim text.",
        )
    from app.agents.adzump.agents.creative.agent import get_creative_agent
    result = await get_creative_agent().handle(user_message, context)
    logger.info("manage_creatives tool returning result: success=%s, audience=%s, summary_len=%d",
                result.success, getattr(result, 'audience', None), len(result.summary or ""))
    return result


manage_creatives = ToolDefinition(
    name="manage_creatives",
    description=(
        "Handle any request related to ad creative generation — creating new "
        "creatives, editing existing ones, listing creatives, or customizing "
        "them. Always pass the user's verbatim message via `user_message`; "
        "do NOT try to interpret the intent yourself. The creative subsystem "
        "interprets the request internally."
    ),
    display_name="Manage Creatives",
    parameters=[
        ToolParameter(
            name="user_message",
            type="string",
            description=(
                "The user's verbatim message about creatives. Examples: "
                "'generate 3 ad creatives', 'make the second one brighter', "
                "'add a portrait version', 'what creatives do I have?'. "
                "The subsystem interprets intent internally."
            ),
            required=True,
        ),
    ],
    execute=_manage_creatives,
)

CREATIVE_GENERATION_TOOLS = [manage_creatives]
