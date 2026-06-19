from __future__ import annotations

import logging
from typing import Optional, Any, Callable, NamedTuple

from app.core.tools.base import ToolDefinition
from app.agents.adzump.agents.optimization.provider_base import (
    PlatformProvider,
    PlatformCapabilities,
    NEEDS_PRODUCT_MAPPING_REASON,
    ToolSkip,
)
from app.agents.adzump.recommendations.google.capabilities import (
    get_capabilities,
    ChannelCapabilities,
)
from app.agents.adzump.recommendations.models import (
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


class _ChannelGate(NamedTuple):
    supports: Callable[[ChannelCapabilities], bool]  # does this channel support the tool?
    reason: str  # skip-reason template; {label} = campaign-type display label
    section: str  # recommendation section, recorded as a SkippedAnalysis when skipped


# Channel-capability gates, keyed by tool name. Adding a gated tool is ONE entry
# here — never a new branch. Tools absent from this map (budget/bidding,
# conversion health) are channel-agnostic and always apply.
_CHANNEL_GATES: dict[str, _ChannelGate] = {
    "get_keyword_recommendations": _ChannelGate(
        supports=lambda caps: caps.supports_keywords,
        reason="{label} campaigns don't use keywords — Google automates "
        "targeting from asset group signals.",
        section="keywords",
    ),
    # Phase 2 (ad creative) example:
    # "get_ad_creative_recommendations": _ChannelGate(
    #     supports=lambda caps: caps.creative_kind != CreativeKind.NONE,
    #     reason="{label} campaigns don't expose an editable creative structure.",
    #     section="ad_creatives",
    # ),
}


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

    def applicable_tools_for_campaign(
        self, campaign_type: str, has_product_mapping: bool
    ) -> list[tuple[str, Optional[ToolSkip]]]:
        """Google gate — channel-capability rules (table-driven via
        ``_CHANNEL_GATES``) layered on the product-mapping rule. The channel rule
        is checked first so a PMax campaign gets the channel reason regardless of
        mapping; tools with no gate (budget/bidding, conversion health) always
        apply."""
        caps = get_capabilities(campaign_type)
        applicable: list[tuple[str, Optional[ToolSkip]]] = []
        for tool in self.tools:
            skip = None
            gate = _CHANNEL_GATES.get(tool.name)
            if gate and not gate.supports(caps):
                skip = ToolSkip(
                    reason=gate.reason.format(label=caps.display_label),
                    section=gate.section,
                )
            elif getattr(tool, "requires_product_mapping", False) and not has_product_mapping:
                skip = ToolSkip(reason=NEEDS_PRODUCT_MAPPING_REASON)
            applicable.append((tool.name, skip))
        return applicable

    # Adding a tool to this provider — checklist (miss a step and the tool runs
    # but its output silently never persists):
    #   1. Create tools/google/<tool>.py using prepare_google_campaign_tool.
    #   2. Add it to `tools` and `scheduler_tool_order`.
    #   3. If it's channel-gated, add one `_CHANNEL_GATES` entry.
    #   4. Add its output field to GoogleOptimizationFields (models/google.py).
    #   5. Wire its payload into `_assemble_fields` + BOTH extractors below.
    #   6. Render it in craft/google.py.
    def _assemble_fields(
        self,
        overview: Optional[CampaignOverview],
        *,
        campaign_id: str,
        conversion_health: Optional[dict] = None,
        budget_bidding: Optional[dict] = None,
        keywords: Optional[list] = None,
    ) -> GoogleOptimizationFields:
        """Single place that maps raw tool payloads → the typed fields bundle. Both
        flows (scheduler tool_results, chat _fresh_recommendations) normalize their
        source into these kwargs and call this — so the section→model mapping lives
        in ONE spot and the two flows can't drift."""
        fields = GoogleOptimizationFields(
            overview=overview,
            keywords=[GoogleKeywordRecommendation(**k) for k in (keywords or [])],
            budget_bidding=(
                BudgetBiddingRecommendation(**budget_bidding) if budget_bidding else None
            ),
            conversion_health=(
                ConversionHealthReport(**conversion_health) if conversion_health else None
            ),
        )
        if campaign_id:
            self.populate_fingerprints(fields, campaign_id)
        return fields

    def build_fields_from_headless_results(
        self,
        tool_results: dict[str, Any],
        overview: Optional[CampaignOverview],
    ) -> GoogleOptimizationFields:
        def payload(tool: str, key: str) -> Optional[Any]:
            data = tool_results.get(tool)
            if data and data.get("success") and isinstance(data.get("data"), dict):
                return data["data"].get(key)
            return None

        return self._assemble_fields(
            overview,
            campaign_id=getattr(overview, "campaign_id", "") if overview else "",
            conversion_health=payload("verify_conversion_health", "report"),
            budget_bidding=payload("get_budget_bidding_recommendations", "budget_bidding"),
            keywords=payload("get_keyword_recommendations", "keywords"),
        )

    def build_fields_from_session_context(
        self,
        session_context: dict[str, Any],
    ) -> GoogleOptimizationFields:
        campaign_id = session_context.get("active_campaign_id", "")
        fresh = (session_context.get("_fresh_recommendations", {}) or {}).get(
            campaign_id, {}
        ) if campaign_id else {}
        overview_dict = session_context.get("_overview")

        return self._assemble_fields(
            CampaignOverview(**overview_dict) if overview_dict else None,
            campaign_id=campaign_id,
            conversion_health=fresh.get("conversion_health"),
            budget_bidding=fresh.get("budget_bidding"),
            keywords=fresh.get("keywords"),
        )

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
