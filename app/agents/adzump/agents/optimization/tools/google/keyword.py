"""Google keyword recommendation tool.

Sub-agent tool that runs a fresh keyword analysis for a Google Ads campaign.
"""

from __future__ import annotations

import logging

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


async def _get_keyword_recommendations(params: dict, context: dict) -> ToolResult:
    """Run a fresh keyword analysis for one Google Ads campaign."""
    campaign_id = str(params.get("campaign_id") or "").strip()
    logger.info("keyword_recommendations: START campaign=%s", campaign_id)

    # One call resolves account + product mapping AND gates on channel type
    # (PMax/Shopping/etc. have no keywords → honest not-applicable). Returns a
    # ToolResult to hand straight back on any failure or skip.
    from app.agents.adzump.agents.optimization.tools.google._channel import (
        prepare_google_campaign_tool,
    )

    prep = await prepare_google_campaign_tool(
        context,
        campaign_id,
        section="keywords",
        requires_capability=lambda caps: caps.supports_keywords,
        requires_mapping=True,
    )
    if isinstance(prep, ToolResult):
        return prep

    headers = context.get("headers", {})
    client_code = context.get("client_code", "")
    session_ctx = context.get("session_context", {})
    session = context.get("_session")
    account_id = prep.account_id
    login_customer_id = prep.login_customer_id
    mapping = prep.mapping

    try:
        from app.agents.adzump.adapters.google.reporting import (
            google_reporting_adapter,
        )
        from app.agents.adzump.recommendations.google.advisors.keyword import (
            keyword_advisor,
        )
        from app.agents.adzump.recommendations.google.advisors.keyword.evaluator import (
            MetricPerformanceEvaluator,
        )

        logger.info(
            "keyword_recommendations: mapping found campaign=%s product=%s",
            campaign_id,
            mapping.get("product_name", "(unknown)"),
        )

        keywords = await google_reporting_adapter.fetch_keyword_metrics(
            account_id,
            login_customer_id,
            client_code,
            auth_headers=headers,
            campaign_ids=[campaign_id],
        )

        campaign_keywords = [
            kw for kw in keywords if kw["campaign_id"] == campaign_id
        ]

        if not campaign_keywords:
            logger.info(
                "keyword_recommendations: NO keywords found campaign=%s — "
                "campaign may not have active search keywords",
                campaign_id,
            )
            return ToolResult(
                success=True,
                data={"keywords": [], "campaign_id": campaign_id},
                summary=(
                    f"No keyword data found for campaign {campaign_id}. "
                    "The campaign may not have any active keywords yet."
                ),
            )

        evaluator = MetricPerformanceEvaluator()
        scored = evaluator.evaluate(campaign_keywords)
        evaluator.mark_top_performers(scored)

        campaign_group = {
            "campaign_id": campaign_id,
            "name": campaign_keywords[0].get("campaign_name", ""),
            "product_id": mapping.get("product_id", ""),
            "business_summary": mapping.get("summary", ""),
            "business_url": mapping.get("business_url", ""),
            "brand_info": mapping.get("brand_info"),
            "unique_features": mapping.get("unique_features", []),
            "entries": scored,
        }

        recommendations = await keyword_advisor.suggest_keyword_recommendations(
            campaign_group=campaign_group,
            account_id=account_id,
            parent_id=login_customer_id,
            client_code=client_code,
            auth_headers=headers,
            session=session,
        )

    except Exception as e:
        logger.error(
            "keyword_recommendations: FAILED campaign=%s error=%s",
            campaign_id,
            str(e),
            exc_info=True,
        )
        return ToolResult(
            success=False,
            error=(
                f"Keyword analysis for campaign {campaign_id} failed due to a data "
                "retrieval issue. The Google Ads API may be temporarily unavailable. "
                "Please try again shortly."
            ),
        )

    pause_recs = [r for r in recommendations if r.recommendation == "PAUSE"]
    add_recs = [r for r in recommendations if r.recommendation == "ADD"]

    summary_lines = [
        f"Keyword Analysis: campaign {campaign_id}",
        f"Total: {len(recommendations)} recommendations "
        f"({len(pause_recs)} PAUSE, {len(add_recs)} ADD)",
        "",
    ]

    if pause_recs:
        summary_lines.append("PAUSE recommendations:")
        for r in pause_recs:
            summary_lines.append(f"  '{r.text}' — {r.reason}")
        summary_lines.append("")

    if add_recs:
        summary_lines.append("ADD recommendations (top 5):")
        for r in add_recs[:5]:
            line = f"  '{r.text}' ({r.match_type})"
            if r.score:
                line += f" — score: {r.score:.1f}"
            summary_lines.append(line)
        if len(add_recs) > 5:
            summary_lines.append(f"  ... and {len(add_recs) - 5} more")

    session_ctx.setdefault("_fresh_recommendations", {}).setdefault(
        campaign_id, {}
    )["keywords"] = [r.model_dump() for r in recommendations]

    logger.info(
        "get_keyword_recommendations done: campaign=%s pause=%d add=%d",
        campaign_id, len(pause_recs), len(add_recs),
    )

    return ToolResult(
        success=True,
        data={"keywords": [r.model_dump() for r in recommendations]},
        summary="\n".join(summary_lines),
    )


get_keyword_recommendations = ToolDefinition(
    name="get_keyword_recommendations",
    description=(
        "Run a fresh keyword analysis for a specific Google Ads campaign. "
        "Returns PAUSE recommendations for critically poor keywords and "
        "ADD recommendations for new keyword opportunities. "
        "Call this when the user asks for a fresh keyword analysis, "
        "not when serving stored recommendations from storage."
    ),
    display_name="Get Keyword Recommendations",
    parameters=[
        ToolParameter(
            name="campaign_id",
            type="string",
            description="Google Ads campaign ID to run keyword analysis for.",
            required=True,
        ),
    ],
    execute=_get_keyword_recommendations,
    requires_product_mapping=True,
)
