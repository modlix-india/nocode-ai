"""Meta Ads visual layout renderer.

Registered as ``META`` in ``PlatformCraftRegistry``.  Converts a
``CampaignRecommendation`` into Craft blocks using type-safe builders.

Key differences from Google:
  - Uses "Pixel & Signal Diagnostics" instead of "Conversion Health"
  - Uses "Audience Placements & Budget" instead of "Budget & Bidding"
  - No keyword sections (Meta uses demographic/behavioral targeting)
  - Age/gender use adset_name, recommended_min/max instead of ad_group/age_range

TODO (Meta Integration):
  - CampaignOverview and dynamic currency formatting are not yet integrated for Meta.
  - Diagnostic Fixes cards and suggestion list blocks for Pixel health are pending Meta Graph API tool implementation.
  - Currently uses legacy fallback UI blocks until the Meta adapter supports lightweight metric extraction.
"""

from __future__ import annotations

import logging

from app.agents.adzump.agents.optimization.craft.base import (
    PlatformCraftRenderer,
    PlatformCraftRegistry,
)
from app.agents.adzump.agents.optimization.craft.builders import (
    badge_block,
    callout_block,
    divider_block,
    heading_block,
    key_value_block,
    metric_block,
    row_block,
    suggestion_item,
    suggestion_list_block,
    suggestion_section,
    text_block,
)
from app.agents.adzump.agents.optimization.craft.google import _safe_float_format
from app.agents.adzump.recommendations.models import (
    CampaignRecommendation,
    CheckSeverity,
)

logger = logging.getLogger(__name__)


@PlatformCraftRegistry.register("META")
class MetaCraftRenderer(PlatformCraftRenderer):
    """Meta Ads full and compact visual layout builder."""

    # ── Full render (single-campaign detail view) ────────────────────────

    def render_recommendation_blocks(self, rec: CampaignRecommendation) -> list[dict]:
        blocks: list[dict] = []

        # Suggestion list (age, gender — Meta doesn't use keywords)
        sections = self._build_suggestion_sections(rec)
        if sections:
            blocks.append(
                suggestion_list_block(rec.campaign_id, rec.campaign_name, sections)
            )

        # Pixel & signal diagnostics
        self._append_pixel_diagnostics(blocks, rec, has_sections=bool(sections))

        # Budget & bid caps
        self._append_budget_bidding(blocks, rec)

        # Campaign metadata footer
        self._append_metadata(blocks, rec)

        return blocks

    # ── Compact render (multi-campaign accordion child) ──────────────────

    def render_compact_blocks(self, rec: CampaignRecommendation) -> list[dict]:
        blocks: list[dict] = []

        # Suggestion list
        sections = self._build_suggestion_sections(rec)
        if sections:
            blocks.append(
                suggestion_list_block(rec.campaign_id, rec.campaign_name, sections)
            )

        # Compact pixel health
        ch = rec.fields.conversion_health
        if ch:
            health_score = ch.health_score if ch.health_score is not None else 100
            health_label = ch.health_label or "HEALTHY"
            failing = [c for c in ch.checks if not c.passed]
            if failing:
                blocks.append(
                    callout_block(
                        f"Pixel Health Score: {health_score}/100 ({health_label}) — "
                        f"{len(failing)} failing checks.",
                        "warning",
                    )
                )
            else:
                blocks.append(
                    callout_block(
                        f"Pixel Health Score: {health_score}/100 ({health_label}) — "
                        f"All checks passed.",
                        "success",
                    )
                )

        # Compact budget metric
        bb = rec.fields.budget_bidding
        if bb:
            curr = _safe_float_format(bb.current_budget)
            rec_bud = (
                _safe_float_format(bb.recommended_budget)
                if bb.recommended_budget is not None
                else curr
            )
            blocks.append(
                row_block([
                    metric_block(
                        "Daily Budget",
                        f"${curr}",
                        f"Recommended: ${rec_bud}" if bb.recommended_budget is not None else "Optimized pacing",
                    ),
                ])
            )
            blocks.append(
                key_value_block([
                    {"key": "Campaign ID", "value": rec.campaign_id},
                    {"key": "Ad Strategy", "value": bb.current_strategy or "Lowest Cost"},
                ])
            )

        # Fallback if completely empty
        if not sections and not bb and not ch:
            blocks.append(
                callout_block("All components verified, no suggestions at this time.", "success")
            )

        return blocks

    # ── Private section builders ─────────────────────────────────────────

    def _build_suggestion_sections(self, rec: CampaignRecommendation) -> list[dict]:
        """Build Meta-specific suggestion sections (age, gender — no keywords)."""
        sections: list[dict] = []

        # Age Targeting (uses recommended_min/max instead of age_range)
        if rec.fields.age:
            items = []
            for idx, a in enumerate(rec.fields.age, 1):
                action = (a.recommendation or a.action or "UPDATE").upper()
                if "_" in action:
                    action = action.split("_")[0]

                # Meta uses min/max ranges instead of Google's AGE_RANGE_* enum
                label = ""
                if a.age_range:
                    label = a.age_range.replace("AGE_RANGE_", "").replace("_", "-")
                elif a.recommended_min is not None and a.recommended_max is not None:
                    label = f"{a.recommended_min}-{a.recommended_max} Yrs"
                elif a.current_min is not None and a.current_max is not None:
                    label = f"{a.current_min}-{a.current_max} Yrs"
                else:
                    label = "Targeted Age"

                sublabel = f"Ad Set: {a.adset_name}" if a.adset_name else "Targeting Segment"

                items.append(
                    suggestion_item(
                        f"age_{idx}_{label.lower().replace(' ', '_')}",
                        label, sublabel, action, a.reason or "",
                    )
                )
            sections.append(suggestion_section("Age Targeting", items))

        # Gender Targeting
        if rec.fields.gender:
            items = []
            for idx, g in enumerate(rec.fields.gender, 1):
                action = (g.recommendation or g.action or "UPDATE").upper()
                if "_" in action:
                    action = action.split("_")[0]

                gender_val = g.gender or g.gender_type or "UNDETERMINED"
                label = gender_val.replace("_", " ").title()
                sublabel = f"Ad Set: {g.adset_name}" if g.adset_name else "Targeting Segment"

                items.append(
                    suggestion_item(
                        f"gender_{idx}_{gender_val.lower()}",
                        label, sublabel, action, g.reason or "",
                    )
                )
            sections.append(suggestion_section("Gender Targeting", items))

        # Note: Meta Ads does not use keywords. It relies on demographic,
        # behavioral, and pixel-based lookalike/retargeting audiences.
        # Keyword sections are intentionally omitted.

        return sections

    def _append_pixel_diagnostics(
        self, blocks: list[dict], rec: CampaignRecommendation, *, has_sections: bool
    ) -> None:
        """Append Pixel & Signal Diagnostics section."""
        ch = rec.fields.conversion_health
        if ch:
            health_score = ch.health_score if ch.health_score is not None else 100
            health_label = ch.health_label or "HEALTHY"

            blocks.append(divider_block())
            blocks.append(heading_block("Pixel & Signal Diagnostics", level=2))
            blocks.append(badge_block(f"Health Score: {health_score}/100 ({health_label})"))

            failing = [check for check in ch.checks if not check.passed]
            if failing:
                for check in failing:
                    variant = "danger" if check.severity == CheckSeverity.CRITICAL else "warning"
                    blocks.append(
                        callout_block(f"Failing Check: {check.title} — {check.description}", variant)
                    )
            else:
                blocks.append(
                    callout_block(
                        "Meta Pixel and Signal setup fully operational. All tracking events are active.",
                        "success",
                    )
                )
        elif not has_sections:
            blocks.append(
                callout_block("No Pixel diagnostic reports found for this campaign.", "info")
            )

    def _append_budget_bidding(self, blocks: list[dict], rec: CampaignRecommendation) -> None:
        """Append Audience Placements & Budget section."""
        bb = rec.fields.budget_bidding
        if not bb:
            return

        blocks.append(divider_block())
        blocks.append(heading_block("Audience Placements & Budget", level=2))

        curr = _safe_float_format(bb.current_budget)
        rec_bud = (
            _safe_float_format(bb.recommended_budget)
            if bb.recommended_budget is not None
            else curr
        )

        blocks.append(
            row_block([
                metric_block("Current Budget", f"${curr}", "Active daily pacing"),
                metric_block("Recommended Budget", f"${rec_bud}", "Optimized placement limits"),
            ])
        )

        kv_bb = [{"key": "Ad Strategy", "value": bb.current_strategy}]
        if bb.bidding_rec_type:
            kv_bb.append({"key": "Recommendation", "value": bb.bidding_rec_type})
        blocks.append(key_value_block(kv_bb))

        if bb.bidding_rec_rationale or bb.budget_rec_rationale:
            parts = []
            if bb.bidding_rec_rationale:
                parts.append(f"Audience targeting: {bb.bidding_rec_rationale}")
            if bb.budget_rec_rationale:
                parts.append(f"Placement budget: {bb.budget_rec_rationale}")
            blocks.append(text_block("\n".join(parts)))

        if bb.learning_phase_warning:
            blocks.append(callout_block(f"Learning Phase: {bb.learning_phase_warning}", "warning"))
        if bb.blocking_issues:
            blocks.append(callout_block(f"Blocking Issues: {', '.join(bb.blocking_issues)}", "danger"))

    def _append_metadata(self, blocks: list[dict], rec: CampaignRecommendation) -> None:
        """Append campaign metadata footer."""
        blocks.append(divider_block())
        kv_meta = [
            {"key": "Campaign ID", "value": rec.campaign_id},
            {"key": "Campaign Type", "value": rec.campaign_type},
            {"key": "Account ID", "value": rec.account_id},
            {"key": "Platform", "value": "Meta Ads"},
        ]
        if rec.generated_at:
            kv_meta.append({"key": "Generated At", "value": rec.generated_at})
        blocks.append(key_value_block(kv_meta))
