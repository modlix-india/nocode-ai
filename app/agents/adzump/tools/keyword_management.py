"""Keyword question / edit routing for Adzump.

The orchestrator routes keyword requests here; the KeywordResearchAgent's own
agentic loop (handle → lookup/edit tools) decides what to answer or change.
"""

from __future__ import annotations

import logging

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.agents.campaign.google.keyword.agent import get_keyword_manage_agent

logger = logging.getLogger(__name__)


async def _manage_keywords(params: dict, context: dict) -> ToolResult:
    """Orchestrator-facing entry point for keyword questions and edits.

    The orchestrator (Adzump) is a pure router - it does NOT answer keyword questions or
    decide which edit to make. It routes "this is about the keywords" here with the user's
    verbatim message. ``KeywordResearchAgent.handle()`` runs the keyword agent's own loop,
    which reads the record it wrote during research and acts.

    The keyword agent is the expert and holds the reasons; answering here would mean
    inventing them.
    """
    user_message = (params.get("user_message") or "").strip()
    if not user_message:
        return ToolResult(
            success=False,
            error=(
                "manage_keywords requires a `user_message` - the orchestrator should "
                "forward the user's verbatim text. Empty messages are rejected here so "
                "the orchestrator gets a clean retry signal."
            ),
        )
    return await get_keyword_manage_agent().handle(user_message, context)


manage_keywords = ToolDefinition(
    name="manage_keywords",
    description=(
        "Handle anything the user says about the researched keywords - a question "
        "(\"why is X in there?\", \"why isn't Y?\", \"is X too broad?\") or a change "
        "(\"add keywords for the locations\", \"include apartment terms\", \"drop the "
        "low-volume ones\"). Always pass their verbatim message via `user_message`; do "
        "NOT answer keyword questions yourself and do NOT classify the request - the "
        "keyword agent recorded why each keyword was chosen or skipped, and it decides "
        "what to do. Only route here once keywords exist."
    ),
    display_name="Keywords",
    parameters=[
        ToolParameter(
            name="user_message",
            type="string",
            description=(
                "The user's verbatim message about the keywords. Examples: 'why did you "
                "include affordable running shoes?', 'why isn't cheap shoes there?', "
                "'add some keywords for the locations', 'remove the low volume ones'."
            ),
            required=True,
        ),
        # NOTE: which keyword, which ad group, add-vs-remove, match types are deliberately
        # NOT exposed. They belong to the keyword agent's own loop, which extracts them from
        # the user's message. Exposing them here would let the orchestrator fabricate
        # structured edits with no traceability check (same rule as manage_targeting_locations).
    ],
    execute=_manage_keywords,
)

KEYWORD_TOOLS = [manage_keywords]
