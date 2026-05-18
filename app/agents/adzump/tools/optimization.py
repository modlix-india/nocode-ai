"""Search term optimization tool for the main Adzump chat agent."""

from __future__ import annotations

import logging
from app.core.tools.base import ToolDefinition, ToolResult
from app.agents.adzump.agents.optimization.google.search_terms.agent import (
    get_search_term_optimization_agent,
)

logger = logging.getLogger(__name__)


async def _optimize_search_terms(params: dict, context: dict) -> ToolResult:
    session_ctx = context.get("session_context") or {}
    client_code = session_ctx.get("client_code") or context.get("client_code")

    if not client_code:
        return ToolResult(
            success=False,
            error="client_code not found. Please ensure account is connected.",
        )

    agent = get_search_term_optimization_agent()
    results = await agent.generate_recommendations(context)

    recommendations = results.get("recommendations", [])
    count = len(recommendations)

    if count == 0:
        return ToolResult(
            success=True,
            data=results,
            summary=f"I analyzed the search terms for client '{client_code}', but no new recommendations were found at this time. This usually means your current keywords are already well-optimized or there isn't enough new search traffic to analyze yet.",
        )

    def sanitize(text: str | None) -> str:
        if not text:
            return ""
        return str(text).replace("|", "-").replace("\n", " ")

    # Build a markdown table for the UI
    table = [
        "| Campaign | Match Type | Recommended | Keyword | Reason |",
        "|---|---|---|---|---|"
    ]
    for rec in recommendations:
        campaign_name = sanitize(rec.get("campaign_name", "Unknown Campaign"))
        fields = rec.get("fields", {})

        # Add positive keywords
        for kw in fields.get("keywords") or []:
            match_type = sanitize(kw.get("match_type", "Broad"))
            text = sanitize(kw.get("text"))
            reason = sanitize(kw.get("reason"))
            table.append(
                f"| {campaign_name} | {match_type} | Positive | {text} | {reason} |"
            )

        # Add negative keywords
        for kw in fields.get("negativeKeywords") or []:
            match_type = sanitize(kw.get("match_type", "Negative"))
            text = sanitize(kw.get("text"))
            reason = sanitize(kw.get("reason"))
            table.append(
                f"| {campaign_name} | {match_type} | Negative | {text} | {reason} |"
            )

    # The summary_text is what the LLM sees first. We make it JUST the table.
    summary_text = "\n".join(table)

    # Add the table to the data as well
    results["markdown_table"] = summary_text

    return ToolResult(
        success=True,
        data=results,
        summary=summary_text,
    )


optimize_search_terms = ToolDefinition(
    name="optimize_search_terms",
    description=(
        "Analyze Google Ads search term performance and generate keyword recommendations. "
        "MANDATORY: You MUST output the results as a Markdown table. Do NOT use bullet points or lists. "
        "Do NOT ask for a business URL — campaigns are already mapped. "
        "The tool returns a 'markdown_table' in the 'data' and 'summary' fields. "
        "You MUST copy and paste the full 'markdown_table' into your final response. "
        "Do NOT summarize or omit the table."
    ),
    display_name="Optimize Search Terms",
    execute=_optimize_search_terms,
)

OPTIMIZATION_TOOLS = [optimize_search_terms]
