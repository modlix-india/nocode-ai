"""Google budget and bidding recommendation tool.

Sub-agent tool that runs a fresh budget/bidding analysis for a Google Ads campaign.
"""

from __future__ import annotations

import logging

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


async def _get_budget_bidding_recommendations(params: dict, context: dict) -> ToolResult:
    """Run a fresh budget and bidding strategy analysis for one Google Ads campaign."""
    campaign_id = str(params.get("campaign_id") or "").strip()
    logger.info("budget_bidding: START campaign=%s", campaign_id)

    from app.agents.adzump.agents.optimization.tools.google._channel import (
        prepare_google_campaign_tool,
    )

    prep = await prepare_google_campaign_tool(
        context, campaign_id, section="budget_bidding"
    )
    if isinstance(prep, ToolResult):
        return prep

    headers = context.get("headers", {})
    client_code = context.get("client_code", "")
    session_ctx = context.get("session_context", {})
    account_id = prep.account_id
    login_customer_id = prep.login_customer_id

    try:
        from app.agents.adzump.recommendations.google.advisors.budget import (
            budget_bidding_advisor,
        )

        tool_context = {
            "headers": headers,
            "client_code": client_code,
            "_session": context.get("_session"),
            "auth": context.get("auth"),
        }

        recs = await budget_bidding_advisor.analyse(
            account_id=account_id,
            parent_id=login_customer_id,
            client_code=client_code,
            context=tool_context,
            campaign_ids=[campaign_id],
        )

    except Exception as e:
        logger.error(
            "budget_bidding: FAILED campaign=%s error=%s",
            campaign_id,
            str(e),
            exc_info=True,
        )
        return ToolResult(
            success=False,
            error=(
                f"Budget analysis for campaign {campaign_id} failed due to a data "
                "retrieval issue. The Google Ads API may be temporarily unavailable. "
                "Please try again shortly."
            ),
        )

    if not recs:
        logger.info(
            "budget_bidding: NO data found campaign=%s — campaign may be inactive",
            campaign_id,
        )
        return ToolResult(
            success=True,
            data={"budget_bidding": None, "campaign_id": campaign_id},
            summary=(
                f"No budget or bidding data available for campaign {campaign_id}. "
                "The campaign may not be active or may not have enough data yet."
            ),
        )

    rec = recs[0]

    summary_lines = [
        f"Budget & Bidding Analysis: {rec.campaign_name} ({campaign_id})",
        f"Current strategy: {rec.current_strategy}",
        f"Current budget: {rec.current_budget:.2f}",
        "",
    ]

    if rec.bidding_rec_type:
        summary_lines.append(
            f"Bidding recommendation: {rec.bidding_rec_type} "
            f"(confidence: {rec.bidding_confidence})"
        )
        summary_lines.append(f"Rationale: {rec.bidding_rec_rationale}")
        if rec.google_rec_confirmed:
            summary_lines.append("Confirmed by Google's native recommendations")
    else:
        summary_lines.append("No bidding strategy change recommended.")

    summary_lines.append("")

    if rec.recommended_budget:
        summary_lines.append(
            f"Budget recommendation: {rec.current_budget:.2f} -> {rec.recommended_budget:.2f} "
            f"(confidence: {rec.budget_confidence})"
        )
        summary_lines.append(f"Rationale: {rec.budget_rec_rationale}")
        if rec.pacing_warning:
            summary_lines.append(f"Pacing: {rec.pacing_warning}")
    else:
        summary_lines.append(f"No budget change recommended. Constraint: {rec.constraint_type}")

    if rec.learning_phase_warning:
        summary_lines.append(f"Learning phase: {rec.learning_phase_warning}")
    if rec.metric_freshness_warning:
        summary_lines.append(f"Data freshness: {rec.metric_freshness_warning}")
    if rec.blocking_issues:
        for issue in rec.blocking_issues:
            summary_lines.append(f"{issue}")

    if rec.apply_order:
        summary_lines.append(f"\nApply order: {' -> '.join(rec.apply_order)}")

    session_ctx.setdefault("_fresh_recommendations", {}). \
        setdefault(campaign_id, {})["budget_bidding"] = rec.model_dump()

    logger.info(
        "get_budget_bidding_recommendations done: campaign=%s bidding=%s budget=%s",
        campaign_id,
        rec.bidding_rec_type or "none",
        f"{rec.recommended_budget:.2f}" if rec.recommended_budget else "none",
    )

    return ToolResult(
        success=True,
        data={"budget_bidding": rec.model_dump()},
        summary="\n".join(summary_lines),
    )


get_budget_bidding_recommendations = ToolDefinition(
    name="get_budget_bidding_recommendations",
    description=(
        "Run a fresh budget and bidding strategy analysis for a specific Google Ads campaign. "
        "Evaluates conversion volume, impression share signals, constraint type, "
        "and native Google Ads recommendations to produce a BudgetBiddingRecommendation. "
        "Call this when the user asks for a fresh budget or bidding analysis, "
        "not when serving stored recommendations from storage."
    ),
    display_name="Get Budget & Bidding Recommendations",
    parameters=[
        ToolParameter(
            name="campaign_id",
            type="string",
            description="Google Ads campaign ID to run budget/bidding analysis for.",
            required=True,
        ),
    ],
    execute=_get_budget_bidding_recommendations,
)
