from __future__ import annotations

from structlog import get_logger

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.agents.optimization.google.search_terms.metrics import (
    analyze_search_term_metrics,
)

logger = get_logger(__name__)


async def _analyze_search_terms(params: dict, context: dict) -> ToolResult:
    business_summary = params.get("business_summary", "")
    search_terms = params.get("search_terms", [])
    campaign_name = params.get("campaign_name", "Unknown")

    if not search_terms:
        return ToolResult(success=True, data={"recommendations": [], "count": 0})

    from app.agents.adzump.agents.optimization.google.search_terms.analyzer import (
        get_search_term_analyzer,
    )

    analyzer = await get_search_term_analyzer()
    llm_results = await analyzer.analyze(
        business_summary,
        search_terms,
        campaign_name=campaign_name
    )

    # Map LLM results back to the original search terms to preserve metrics and IDs
    # Use normalized keys (lowercase/stripped) to handle minor LLM formatting variations
    term_map = {t["search_term"].strip().lower(): t for t in search_terms}
    
    recommendations = []
    for llm_res in llm_results:
        text = llm_res.get("text", "").strip()
        normalized_text = text.lower()
        original_term = term_map.get(normalized_text)
        
        if not original_term:
            logger.warning(
                "llm_term_match_failed",
                returned_text=text,
                available_terms=list(term_map.keys())[:10]
            )
            continue

        recommendation_type = llm_res.get("recommendation_type", "negative")

        recommendations.append(
            {
                "text": text,
                "match_type": original_term.get("match_type", "BROAD"),
                "recommendation_type": recommendation_type,
                "reason": llm_res.get("reason"),
                "metrics": original_term.get("metrics"),
                "ad_group_id": original_term.get("ad_group_id"),
                "ad_group_name": original_term.get("ad_group_name"),
                "analysis": {
                    "match": recommendation_type == "positive",
                    "match_level": llm_res.get("match_level", "Medium"),
                    "intent_stage": llm_res.get("intent_stage", "Unknown"),
                    "suggestion_type": recommendation_type,
                    "business_summary_used": True,
                },
            }
        )

    # Fallback to metrics if LLM returned nothing (to ensure some response)
    if not recommendations:
        logger.info("LLM analysis returned no recommendations, falling back to metrics")
        metric_results = await analyze_search_term_metrics(search_terms)
        for result in metric_results:
            recommendation_type = result.get("recommendation_type", "negative")
            recommendations.append(
                {
                    "text": result.get("text"),
                    "match_type": result.get("match_type"),
                    "recommendation_type": recommendation_type,
                    "reason": result.get("reason"),
                    "metrics": result.get("metrics"),
                    "ad_group_id": result.get("ad_group_id"),
                    "ad_group_name": result.get("ad_group_name"),
                    "analysis": {
                        "match": recommendation_type == "positive",
                        "match_level": "Medium",
                        "intent_stage": "Metric Based",
                        "suggestion_type": recommendation_type,
                        "business_summary_used": False,
                    },
                }
            )

    return ToolResult(
        success=True,
        data={"recommendations": recommendations, "count": len(recommendations)},
    )


analyze_search_terms = ToolDefinition(
    name="analyze_search_terms",
    description="Analyze Google Ads search terms and generate positive or negative keyword recommendations.",
    parameters=[
        ToolParameter(
            name="business_summary",
            type="string",
            description="Business summary for relevance analysis",
            required=True,
        ),
        ToolParameter(
            name="campaign_name",
            type="string",
            description="The name of the campaign being analyzed (used for brand protection)",
            required=False,
        ),
        ToolParameter(
            name="search_terms",
            type="array",
            description="List of search term dicts to analyze",
            required=True,
            items={"type": "object"},
        ),
    ],
    execute=_analyze_search_terms,
)
