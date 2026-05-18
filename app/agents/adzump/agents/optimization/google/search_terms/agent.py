from __future__ import annotations

import asyncio

from structlog import get_logger

from app.core.agent import BaseAgent
from app.config import settings

from app.agents.adzump.adapters.google.accounts import GoogleAccountsAdapter

from app.agents.adzump.services.campaign_mapping import (
    get_campaign_mapping_with_summary,
)

from app.agents.adzump.services.recommendation_storage import (
    store_recommendation,
)

from app.agents.adzump.agents.optimization.google.search_terms.context import (
    build_search_term_context,
)

from app.agents.adzump.models.optimization import (
    CampaignRecommendation,
    KeywordRecommendation,
    OptimizationFields,
    SearchTermAnalysis,
    SearchTermMetrics,
    KEYWORD_MAX_LENGTH,
)


logger = get_logger(__name__)


class SearchTermOptimizationAgent(BaseAgent):
    display_name = "Search Term Optimization Agent"

    _instance: "SearchTermOptimizationAgent | None" = None

    def __init__(self) -> None:

        context = build_search_term_context()

        context._cached_static_text = context._static_prefix

        provider = getattr(settings, "ADZUMP_PROVIDER", settings.LLM_PROVIDER)
        from app.agents.adzump.agents.optimization.google.search_terms.tools import (
            SEARCH_TERM_TOOLS,
        )

        super().__init__(
            name="adzump_optimization",
            tools=SEARCH_TERM_TOOLS,
            context_builder=context,
            provider=provider,
            model_tier="balanced",
        )

    @classmethod
    def get_instance(cls) -> SearchTermOptimizationAgent:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def generate_recommendations(self, ctx: dict) -> dict:
        session_ctx = ctx.get("session_context") or {}
        client_code = ctx.get("client_code") or session_ctx.get("client_code", "")
        auth_headers = ctx.get("headers") or session_ctx.get("headers") or {}

        accounts_adapter = GoogleAccountsAdapter()
        accounts = await accounts_adapter.fetch_accessible_accounts(client_code, auth_headers)

        if not accounts:
            logger.warning("no_connected_accounts_found")
            return {"recommendations": []}

        # Returns (campaign_mapping, fallback_entry)
        campaign_mapping, fallback_entry = await get_campaign_mapping_with_summary(ctx)

        # 1. FETCH ALL DATA PRIVATELY (Silent)
        # ------------------------------------
        from app.agents.adzump.agents.optimization.google.search_terms.tools.fetch_search_terms import (
            _fetch_search_terms,
        )
        from app.agents.adzump.agents.optimization.google.search_terms.tools.analyze_term import (
            _analyze_search_terms,
        )

        tool_result = await _fetch_search_terms(
            {"accounts": accounts},
            ctx,
        )
        
        all_search_terms = tool_result.data.get("search_terms", [])
        logger.info("fetched_search_terms", search_terms=all_search_terms, count=len(all_search_terms))
        if not all_search_terms:
            return {"recommendations": []}

        # 2. GROUP AND ANALYZE PRIVATELY
        # -----------------------------
        campaigns = self._group_by_campaign(all_search_terms)
        all_recommendations = []

        for campaign_id, campaign_data in campaigns.items():
            mapping = campaign_mapping.get(campaign_id) or fallback_entry
            if not mapping or not mapping.get("summary"):
                continue

            analysis_result = await _analyze_search_terms(
                {
                    "business_summary": mapping["summary"],
                    "campaign_name": campaign_data["name"],
                    "search_terms": campaign_data["terms"],
                },
                ctx,
            )

            recommendation_payloads = analysis_result.data.get("recommendations", [])

            positive_keywords = []
            negative_keywords = []

            for payload in recommendation_payloads:
                text = payload.get("text")
                if not text or len(text) > KEYWORD_MAX_LENGTH:
                    continue

                recommendation = KeywordRecommendation(
                    text=text,
                    match_type=payload.get("match_type", "BROAD"),
                    reason=payload.get("reason", ""),
                    metrics=SearchTermMetrics(**payload.get("metrics", {})),
                    analysis=SearchTermAnalysis(
                        **{
                            k: v
                            for k, v in payload.get("analysis", {}).items()
                            if k in SearchTermAnalysis.model_fields
                        }
                    ),
                    ad_group_id=payload.get("ad_group_id"),
                    ad_group_name=payload.get("ad_group_name"),
                )

                if payload.get("recommendation_type") == "positive":
                    positive_keywords.append(recommendation)
                else:
                    negative_keywords.append(recommendation)

            if positive_keywords or negative_keywords:
                all_recommendations.append(
                    CampaignRecommendation(
                        platform="GOOGLE",
                        parent_account_id=campaign_data["parent_account_id"],
                        account_id=campaign_data["account_id"],
                        product_id=mapping.get("product_id", "unknown"),
                        campaign_id=campaign_id,
                        campaign_name=campaign_data["name"],
                        campaign_type="SEARCH",
                        fields=OptimizationFields(
                            keywords=positive_keywords,
                            negativeKeywords=negative_keywords,
                        ),
                    )
                )

        # 3. STORE AND RETURN
        # -------------------
        for rec in all_recommendations:
            rec_dump = rec.model_dump()
            logger.info("storing_recommendation", recommendation=rec_dump)
            await store_recommendation(rec, ctx)

        return {
            "recommendations": [
                recommendation.model_dump() for recommendation in all_recommendations
            ]
        }

    def _group_by_campaign(self, search_terms: list[dict]) -> dict:
        campaigns = {}
        for term in search_terms:
            campaign_id = term.get("campaign_id")
            if not campaign_id:
                continue

            if campaign_id not in campaigns:
                campaigns[campaign_id] = {
                    "name": term.get("campaign_name", "Unknown"),
                    "account_id": term.get("account_id"),
                    "parent_account_id": term.get("parent_account_id"),
                    "terms": [],
                }
            campaigns[campaign_id]["terms"].append(term)
        return campaigns


def get_search_term_optimization_agent() -> SearchTermOptimizationAgent:
    return SearchTermOptimizationAgent.get_instance()
