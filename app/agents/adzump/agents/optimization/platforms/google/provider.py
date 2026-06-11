from __future__ import annotations

import logging
from typing import Optional, Any

from app.core.tools.base import ToolDefinition
from app.agents.adzump.agents.optimization.provider_base import (
    PlatformProvider,
    PlatformCapabilities,
)
from app.agents.adzump.agents.optimization.models import (
    BaseOptimizationFields,
    GoogleOptimizationFields,
    CampaignOverview,
    GoogleKeywordRecommendation,
    BudgetBiddingRecommendation,
    ConversionHealthReport,
)

logger = logging.getLogger(__name__)


PLATFORM_INSTRUCTIONS = """
When conversion_signal status is "dropping" or "untracked", verify conversion \
health BEFORE recommending any conversion-dependent bidding strategy. Explain \
the tracking issue and surface fix cards. Keyword and budget recommendations \
are still valid — present them alongside the health report.

When presenting recommendations, always include the conversion health status. \
If health_label is CRITICAL or WARNING, mention it before bidding recommendations \
and explain why certain strategies need tracking fixed first.

- Never recommend TARGET_ROAS without first checking conversion value is configured
- Never present a bidding strategy recommendation without mentioning the \
  confidence level (high/medium/low) and the conversion volume it is based on
"""


class GooglePlatformProvider(PlatformProvider):
    @property
    def platform_name(self) -> str:
        return "GOOGLE"

    @property
    def tools(self) -> list[ToolDefinition]:
        # Lazy imports to optimize server boot and load dependencies only when active
        from app.agents.adzump.agents.optimization.tools.google.keyword import (
            get_keyword_recommendations,
        )
        from app.agents.adzump.agents.optimization.tools.google.budget import (
            get_budget_bidding_recommendations,
        )
        from app.agents.adzump.agents.optimization.tools.google.verify_conversion_health import (
            verify_conversion_health,
        )

        return [
            get_keyword_recommendations,
            get_budget_bidding_recommendations,
            verify_conversion_health,
        ]

    @property
    def system_instructions(self) -> str:
        return PLATFORM_INSTRUCTIONS

    @property
    def capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(
            has_recommendations=True,
            has_conversion_signal=True,
            has_scheduler=True,
        )

    @property
    def scheduler_tool_order(self) -> list[str]:
        return [
            "verify_conversion_health",
            "get_budget_bidding_recommendations",
            "get_keyword_recommendations",
        ]

    def build_fields_from_headless_results(
        self,
        tool_results: dict[str, Any],
        overview: Optional[CampaignOverview],
    ) -> GoogleOptimizationFields:
        conversion_health = None
        budget_bidding = None
        keywords = []
        campaign_id = getattr(overview, "campaign_id", "") if overview else ""

        health_data = tool_results.get("verify_conversion_health")
        if (
            health_data
            and health_data.get("success")
            and isinstance(health_data.get("data"), dict)
            and "report" in health_data["data"]
        ):
            conversion_health = ConversionHealthReport(**health_data["data"]["report"])

        bb_data = tool_results.get("get_budget_bidding_recommendations")
        if (
            bb_data
            and bb_data.get("success")
            and isinstance(bb_data.get("data"), dict)
            and bb_data["data"].get("budget_bidding")
        ):
            budget_bidding = BudgetBiddingRecommendation(
                **bb_data["data"]["budget_bidding"]
            )

        kw_data = tool_results.get("get_keyword_recommendations")
        if (
            kw_data
            and kw_data.get("success")
            and isinstance(kw_data.get("data"), dict)
            and kw_data["data"].get("keywords")
        ):
            keywords = [GoogleKeywordRecommendation(**k) for k in kw_data["data"]["keywords"]]

        fields = GoogleOptimizationFields(
            overview=overview,
            keywords=keywords,
            budget_bidding=budget_bidding,
            conversion_health=conversion_health,
        )
        if campaign_id:
            self.populate_fingerprints(fields, campaign_id)
        return fields

    def build_fields_from_session_context(
        self,
        session_context: dict[str, Any],
    ) -> GoogleOptimizationFields:
        campaign_id = session_context.get("active_campaign_id", "")
        fresh_recs = session_context.get("_fresh_recommendations", {})
        campaign_fresh = fresh_recs.get(campaign_id, {}) if campaign_id else {}

        overview_dict = session_context.get("_overview")
        keywords_list = campaign_fresh.get("keywords", [])
        budget_bidding_dict = campaign_fresh.get("budget_bidding")
        conversion_health_dict = campaign_fresh.get("conversion_health")

        overview = CampaignOverview(**overview_dict) if overview_dict else None
        keywords = (
            [GoogleKeywordRecommendation(**k) for k in keywords_list] if keywords_list else []
        )
        budget_bidding = (
            BudgetBiddingRecommendation(**budget_bidding_dict)
            if budget_bidding_dict
            else None
        )
        conversion_health = (
            ConversionHealthReport(**conversion_health_dict)
            if conversion_health_dict
            else None
        )

        fields = GoogleOptimizationFields(
            overview=overview,
            keywords=keywords,
            budget_bidding=budget_bidding,
            conversion_health=conversion_health,
        )
        if campaign_id:
            self.populate_fingerprints(fields, campaign_id)
        return fields

    def merge_fields(
        self,
        existing_fields: Optional[BaseOptimizationFields],
        new_fields: BaseOptimizationFields,
        run_id: str,
        campaign_id: str = "",
    ) -> GoogleOptimizationFields:
        if not campaign_id:
            campaign_id = (
                new_fields.overview.campaign_name
                if (
                    new_fields.overview
                    and getattr(new_fields.overview, "campaign_name", None)
                )
                else ""
            )
            if not campaign_id and new_fields.budget_bidding:
                campaign_id = new_fields.budget_bidding.campaign_id

        from app.agents.adzump.agents.optimization.provider_base import (
            generic_merge_fields,
        )

        return generic_merge_fields(self, existing_fields, new_fields, campaign_id)

    def summarize_fields(self, fields: BaseOptimizationFields) -> list[str]:
        if not isinstance(fields, GoogleOptimizationFields):
            return super().summarize_fields(fields)
        lines: list[str] = []
        if fields.conversion_health:
            ch = fields.conversion_health
            lines.append(
                f"Conversion health: {getattr(ch, 'health_label', 'unknown')} "
                f"({getattr(ch, 'health_score', '?')}/100)"
            )
        bb = fields.budget_bidding
        if bb:
            if bb.bidding_rec_type:
                lines.append(
                    f"Bidding: {bb.bidding_rec_type} (confidence: {bb.bidding_confidence})"
                )
            if bb.recommended_budget:
                lines.append(
                    f"Budget: {bb.current_budget:.2f} → {bb.recommended_budget:.2f}"
                )
        if fields.keywords:
            pause = sum(1 for k in fields.keywords if k.recommendation == "PAUSE")
            add = sum(1 for k in fields.keywords if k.recommendation == "ADD")
            lines.append(f"Keywords: {pause} PAUSE, {add} ADD")
        if fields.age:
            lines.append(f"Age recommendations: {len(fields.age)} items")
        if fields.gender:
            lines.append(f"Gender recommendations: {len(fields.gender)} items")
        return lines
