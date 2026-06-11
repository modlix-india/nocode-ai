"""Keyword Advisor Service.

Orchestrates the full Keyword Optimization pipeline for a campaign:

    1. Track A: Deterministic Poor Keyword Pausing (no LLM, purely rule-based)
    2. Track B: LLM-powered suggestion generation, ranking, and assignment (idea_service)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from app.core.session import BaseSession
from app.agents.adzump.recommendations.google.advisors.keyword.idea_service import (
    idea_service,
)
from app.agents.adzump.agents.optimization.models import GoogleKeywordRecommendation

logger = logging.getLogger(__name__)


class KeywordAdvisorService:
    """Orchestrates keyword suggestions and optimizations for Google Ads campaigns."""

    def __init__(self) -> None:
        self.idea_service = idea_service

    async def suggest_keyword_recommendations(
        self,
        campaign_group: Dict[str, Any],
        account_id: str,
        parent_id: str,
        client_code: str,
        auth_headers: dict,
        session: BaseSession | None = None,
    ) -> List[GoogleKeywordRecommendation]:
        """Run Track A (pause poor keywords) and Track B (suggest new keywords) for a campaign."""
        logger.info(
            "keyword_advisor.suggest_keyword_recommendations: campaign_id=%s",
            campaign_group.get("campaign_id"),
        )

        # 1. Track A: PAUSE critical poor keywords (no LLM — purely deterministic)
        optimizations = self._review_poor_keywords(campaign_group)

        # 2. Build context dict for Track B from the campaign_group's enriched data
        context = {
            "brand_name": (campaign_group.get("brand_info") or {}).get(
                "brand_name", ""
            ),
            "business_type": (campaign_group.get("brand_info") or {}).get(
                "business_type", ""
            ),
            "primary_location": (campaign_group.get("brand_info") or {}).get(
                "primary_location", ""
            ),
            "service_areas": (campaign_group.get("brand_info") or {}).get(
                "service_areas", []
            ),
            "url": campaign_group.get("business_url", ""),
            "unique_features": campaign_group.get("unique_features", []),
            "summary": campaign_group.get("business_summary", ""),
            "headers": auth_headers,
            "client_code": client_code,
        }

        # 3. Track B: LLM-powered suggestion
        suggestions = await self.idea_service.suggest_keywords(
            campaign_details=campaign_group,
            account_id=account_id,
            parent_id=parent_id,
            client_code=client_code,
            context=context,
            session=session,
        )

        total_recs = optimizations + suggestions
        logger.info(
            "keyword_advisor.suggest_keyword_recommendations_done: campaign_id=%s, pause=%d, suggest=%d",
            campaign_group.get("campaign_id"),
            len(optimizations),
            len(suggestions),
        )
        return total_recs

    @staticmethod
    def _review_poor_keywords(campaign_group: Dict[str, Any]) -> List[GoogleKeywordRecommendation]:
        """Track A: PAUSE critical poor keywords.

        No LLM call — evaluator already computes strength, is_critical, and
        reason. Only critical poor keywords (high spend / high clicks with
        zero conversions) get paused; non-critical poor ones are left as-is.
        """
        optimizations: List[GoogleKeywordRecommendation] = []
        for entry in campaign_group.get("entries", []):
            if entry.get("strength") != "poor" or not entry.get("is_critical"):
                continue

            optimizations.append(
                GoogleKeywordRecommendation(
                    text=entry["keyword"],
                    match_type=entry.get("match_type", "PHRASE"),
                    ad_group_id=entry.get("ad_group_id", ""),
                    ad_group_name=entry.get("ad_group_name", ""),
                    criterion_id=entry.get("criterion_id"),
                    resource_name=entry.get("resource_name"),
                    recommendation="PAUSE",
                    reason=entry.get("reason", ""),
                    metrics=entry.get("metrics"),
                    quality_score=entry.get("quality_score"),
                    origin="KEYWORD",
                )
            )

        return optimizations


keyword_advisor = KeywordAdvisorService()
