"""Google Ads visual layout renderer.

Registered as ``GOOGLE`` in ``PlatformCraftRegistry``.  Converts a
``CampaignRecommendation`` into Craft blocks using type-safe builders.

All dict construction goes through ``builders.py`` — no raw dict literals.
"""

from __future__ import annotations

import logging
from typing import Any

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
)
from app.agents.adzump.recommendations.models import (
    CampaignRecommendation,
)

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _safe_float_format(val: Any, decimals: int = 2) -> str:
    """Format floats cleanly, hiding decimals if trailing zeros."""
    if val is None:
        return "0"
    try:
        f = float(val)
        if f.is_integer():
            return f"{int(f):,}"
        return f"{f:,.{decimals}f}"
    except (ValueError, TypeError):
        return str(val)


def _format_currency(val: float, code: str) -> str:
    """Format currency values with proper symbol."""
    fmt = _safe_float_format(val)
    if code == "USD":
        return f"${fmt}"
    elif code == "INR":
        return f"₹{fmt}"
    elif code == "EUR":
        return f"€{fmt}"
    elif code == "GBP":
        return f"£{fmt}"
    return f"{code} {fmt}"


# ── Renderer ─────────────────────────────────────────────────────────────────


@PlatformCraftRegistry.register("GOOGLE")
class GoogleCraftRenderer(PlatformCraftRenderer):
    """Google Ads full and compact visual layout builder."""

    # ── Full render (single-campaign detail view) ────────────────────────

    def render_recommendation_blocks(self, rec: CampaignRecommendation) -> list[dict]:
        blocks: list[dict] = []

        # 1. Top-level Overview (Spend, Conversions, Budget, Health Score)
        self._append_campaign_overview(blocks, rec)
        self._append_budget_summary(blocks, rec)
        self._append_health_summary(blocks, rec)

        # 2. Interactive Suggestion List (contains ALL actionable recommendations)
        sections = self._build_suggestion_sections(rec)
        if sections:
            blocks.append(
                suggestion_list_block(rec.campaign_id, rec.campaign_name, sections)
            )
        elif not rec.fields.conversion_health and not rec.fields.budget_bidding:
            blocks.append(
                callout_block("No actionable recommendations found.", "success")
            )

        return blocks

    # ── Compact render (multi-campaign accordion child) ──────────────────

    def render_compact_blocks(self, rec: CampaignRecommendation) -> list[dict]:
        blocks: list[dict] = []

        # 1. Compact Overview Summaries
        self._append_campaign_overview(blocks, rec)
        self._append_budget_summary(blocks, rec)
        self._append_health_summary(blocks, rec)

        # 2. Interactive Suggestion List
        sections = self._build_suggestion_sections(rec)
        if sections:
            blocks.append(
                suggestion_list_block(rec.campaign_id, rec.campaign_name, sections)
            )

        # Fallback if completely empty
        if (
            not sections
            and not rec.fields.budget_bidding
            and not rec.fields.conversion_health
        ):
            blocks.append(
                callout_block(
                    "All components verified, no suggestions at this time.", "success"
                )
            )

        return blocks

    # ── Private section builders ─────────────────────────────────────────

    def _build_suggestion_sections(self, rec: CampaignRecommendation) -> list[dict]:
        """Build per-field-type suggestion sections for the suggestion_list block."""
        sections: list[dict] = []

        # Age Range
        if rec.fields.age:
            items = []
            for idx, a in enumerate(rec.fields.age, 1):
                action = (a.recommendation or a.action or "REMOVE").upper()
                if "_" in action:
                    action = action.split("_")[0]

                label = ""
                if a.age_range:
                    label = a.age_range.replace("AGE_RANGE_", "").replace("_", "-")
                elif a.recommended_min is not None and a.recommended_max is not None:
                    label = f"{a.recommended_min}-{a.recommended_max} Yrs"
                elif a.current_min is not None and a.current_max is not None:
                    label = f"{a.current_min}-{a.current_max} Yrs"
                else:
                    label = "Targeted Age"

                sublabel = ""
                if a.ad_group_name:
                    sublabel = f"Ad Group: {a.ad_group_name}"
                else:
                    sublabel = "Targeting Segment"

                items.append(
                    suggestion_item(
                        f"age_{idx}_{label.lower().replace(' ', '_')}",
                        label,
                        sublabel,
                        action,
                        a.reason or "",
                    )
                )
            sections.append(suggestion_section("Age Range", items))

        # Gender
        if rec.fields.gender:
            items = []
            for idx, g in enumerate(rec.fields.gender, 1):
                action = (g.recommendation or g.action or "REMOVE").upper()
                if "_" in action:
                    action = action.split("_")[0]

                gender_val = g.gender or g.gender_type or "UNDETERMINED"
                label = gender_val.replace("_", " ").title()

                sublabel = ""
                if g.ad_group_name:
                    sublabel = f"Ad Group: {g.ad_group_name}"
                else:
                    sublabel = "Targeting Segment"

                items.append(
                    suggestion_item(
                        f"gender_{idx}_{gender_val.lower()}",
                        label,
                        sublabel,
                        action,
                        g.reason or "",
                    )
                )
            sections.append(suggestion_section("Gender", items))

        # Keywords
        if rec.fields.keywords:
            items = []
            for idx, kw in enumerate(rec.fields.keywords, 1):
                action = (
                    kw.recommendation or getattr(kw, "action", None) or "ADD"
                ).upper()
                if "_" in action:
                    action = action.split("_")[0]

                match_type = (kw.match_type or "BROAD").upper()
                sublabel_parts = [f"Match: {match_type}"]
                if kw.ad_group_name:
                    sublabel_parts.append(f"Ad Group: {kw.ad_group_name}")
                elif getattr(kw, "adset_name", None):
                    sublabel_parts.append(f"Ad Set: {kw.adset_name}")
                if kw.score is not None:
                    sublabel_parts.append(f"Score: {kw.score:.2f}")

                items.append(
                    suggestion_item(
                        f"kw_{idx}_{kw.text.replace(' ', '_').lower()[:30]}",
                        kw.text,
                        " · ".join(sublabel_parts),
                        action,
                        kw.reason or "",
                    )
                )
            sections.append(suggestion_section("Keywords", items))

        # Negative Keywords
        if rec.fields.negative_keywords:
            items = []
            for idx, kw in enumerate(rec.fields.negative_keywords, 1):
                action = (
                    kw.recommendation or getattr(kw, "action", None) or "REMOVE"
                ).upper()
                if "_" in action:
                    action = action.split("_")[0]

                match_type = (kw.match_type or "BROAD").upper()
                sublabel_parts = [f"Match: {match_type}"]
                if kw.ad_group_name:
                    sublabel_parts.append(f"Ad Group: {kw.ad_group_name}")
                elif getattr(kw, "adset_name", None):
                    sublabel_parts.append(f"Ad Set: {kw.adset_name}")
                if kw.score is not None:
                    sublabel_parts.append(f"Score: {kw.score:.2f}")

                items.append(
                    suggestion_item(
                        f"neg_kw_{idx}_{kw.text.replace(' ', '_').lower()[:30]}",
                        kw.text,
                        " · ".join(sublabel_parts),
                        action,
                        kw.reason or "",
                    )
                )
            sections.append(suggestion_section("Negative Keywords", items))

        # Diagnostic Fixes
        ch = rec.fields.conversion_health
        if ch:
            items = []
            if getattr(ch, "fix_cards", None):
                for idx, card in enumerate(ch.fix_cards, 1):
                    severity_badge = f"[{card.severity.upper()}]"
                    auto_apply = "Auto-Apply" if card.can_auto_apply else "Manual"
                    items.append(
                        suggestion_item(
                            f"fix_{idx}_{card.check_id}",
                            f"{severity_badge} {card.title}",
                            f"Impact: +{card.optiscore_delta}% · {auto_apply}",
                            "APPLY" if card.can_auto_apply else "VIEW",
                            card.rationale,
                        )
                    )
            elif ch.checks:
                # Fallback for old stored recommendations without fix_cards
                for idx, check in enumerate(ch.checks, 1):
                    if not check.passed:
                        severity_badge = (
                            f"[{check.severity.upper()}]"
                            if getattr(check, "severity", None)
                            else "[WARNING]"
                        )
                        items.append(
                            suggestion_item(
                                f"fix_{idx}_{check.check_id}",
                                f"{severity_badge} {check.title or getattr(check, 'name', '') or check.check_id}",
                                "Impact: Manual",
                                "VIEW",
                                check.description or getattr(check, 'message', '') or "",
                            )
                        )
            if items:
                sections.append(suggestion_section("Diagnostic Fixes", items))

        # Budget & Bidding Suggestions
        bb = rec.fields.budget_bidding
        if bb:
            items = []
            currency = bb.currency_code if hasattr(bb, "currency_code") else "INR"
            curr = _format_currency(bb.current_budget, currency)

            # If budget update recommended
            if bb.recommended_budget is not None and round(bb.recommended_budget, 2) != round(
                bb.current_budget, 2
            ):
                rec_bud = _format_currency(bb.recommended_budget, currency)
                items.append(
                    suggestion_item(
                        "budget_update",
                        f"Update daily budget to {rec_bud}",
                        f"Current: {curr} · Strategy: {bb.current_strategy}",
                        "APPLY",
                        bb.budget_rec_rationale
                        or "Optimize delivery based on recent performance.",
                    )
                )

            # If bidding strategy update recommended
            if bb.bidding_rec_type and bb.bidding_rec_type.upper() != "NONE":
                items.append(
                    suggestion_item(
                        "bidding_update",
                        f"Change bidding strategy to {bb.bidding_rec_type.replace('_', ' ').title()}",
                        f"Current: {bb.current_strategy} · Confidence: {bb.bidding_confidence.upper()}",
                        "APPLY",
                        bb.bidding_rec_rationale
                        or "Update bidding strategy to align with campaign goals.",
                    )
                )

            # If we have a diagnosis but no actionable budget amount, surface as VIEW.
            # This covers: BID_CONSTRAINED ("fix Ad Rank first"), MIXED_CONSTRAINT,
            # and BUDGET_CONSTRAINED without a Google native rec (no specific amount).
            constraint = (bb.constraint_type or "NONE").upper()
            if (
                not bb.recommended_budget
                and bb.budget_rec_rationale
                and constraint != "NONE"
            ):
                _constraint_label = {
                    "BUDGET_CONSTRAINED": "Budget Constrained",
                    "BID_CONSTRAINED": "Bid / Quality Score Issue",
                    "MIXED_CONSTRAINT": "Budget & Bid Constraint",
                }.get(constraint, constraint.replace("_", " ").title())
                items.append(
                    suggestion_item(
                        "budget_diagnosis",
                        f"Budget diagnosis: {_constraint_label}",
                        f"Current: {curr}",
                        "VIEW",
                        bb.budget_rec_rationale,
                    )
                )

            if items:
                sections.append(suggestion_section("Budget & Bidding", items))

        return sections

    def _append_campaign_overview(
        self, blocks: list[dict], rec: CampaignRecommendation
    ) -> None:
        """Append top-level campaign KPI block and metadata."""
        blocks.append(heading_block("Campaign Details", level=3))

        kv_meta_1 = [
            {"key": "Campaign ID", "value": rec.campaign_id},
            {"key": "Campaign Type", "value": rec.campaign_type},
            {"key": "Account ID", "value": rec.account_id},
        ]
        kv_meta_2 = [
            {"key": "Platform", "value": "Google Ads"},
            {"key": "Manager ID", "value": rec.parent_account_id or rec.account_id},
        ]
        if rec.generated_at:
            gen_at = rec.generated_at
            if "T" in gen_at:
                try:
                    from datetime import datetime

                    dt = datetime.fromisoformat(gen_at.replace("Z", "+00:00"))
                    gen_at = dt.astimezone().strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    pass
            kv_meta_2.append({"key": "Generated At", "value": gen_at})
        
        blocks.append(
            row_block([
                key_value_block(kv_meta_1),
                key_value_block(kv_meta_2)
            ])
        )

        # Honest "not applicable for this campaign type" callouts next to the type.
        for skip in rec.skipped_analyses:
            blocks.append(
                callout_block(
                    f"{skip.section.replace('_', ' ').title()}: not applicable for "
                    f"{skip.campaign_type.replace('_', ' ').title()} campaigns — "
                    f"{skip.reason}",
                    variant="info",
                )
            )

        ov = rec.fields.overview
        if ov:
            curr = _format_currency(ov.spend, ov.currency_code)
            
            if ov.conversions > 0:
                cpa_val = ov.spend / ov.conversions
                cpa_str = _format_currency(cpa_val, ov.currency_code)
            else:
                cpa_str = "—"

            kpi_row_1 = [
                metric_block("Spend", curr, "Last 30 days"),
                metric_block(
                    "Conversions", _safe_float_format(ov.conversions, 0), "Last 30 days"
                ),
                metric_block("Cost / Conv. (CPA)", cpa_str, "Last 30 days"),
            ]
            
            kpi_row_2 = [
                metric_block("Status", ov.status.replace("_", " ").title(), "Campaign status"),
                metric_block("Ad Sets", str(ov.adset_count), "Active"),
                metric_block("Ads", str(ov.ad_count), "Active"),
            ]
            
            blocks.append(row_block(kpi_row_1))
            blocks.append(row_block(kpi_row_2))

    def _append_health_summary(
        self, blocks: list[dict], rec: CampaignRecommendation
    ) -> None:
        """Append conversion health static summary."""
        ch = rec.fields.conversion_health
        if not ch:
            return

        health_score = ch.health_score if ch.health_score is not None else 100
        if ch.health_label:
            health_label = (
                ch.health_label.name
                if hasattr(ch.health_label, "name")
                else str(ch.health_label).replace("HealthLabel.", "")
            )
        else:
            health_label = "HEALTHY"

        if health_label in ("WARNING", "DEGRADED"):
            variant = "warning"
        elif health_label == "CRITICAL":
            variant = "critical"
        else:
            variant = "neutral"

        blocks.append(divider_block())
        blocks.append(heading_block("Conversion Health", level=2))

        if ch.conversion_signal:
            sig = ch.conversion_signal
            recent = _safe_float_format(sig.conversions_recent, 1)
            prior = _safe_float_format(sig.conversions_prior, 1)

            trend = "down"
            if sig.conversions_recent is not None and sig.conversions_prior is not None:
                try:
                    if float(sig.conversions_recent) >= float(sig.conversions_prior):
                        trend = "up"
                except (ValueError, TypeError):
                    pass

            status_str = sig.status.value if getattr(sig, "status", None) else "UNKNOWN"
            status_formatted = status_str.replace("_", " ").title()

            blocks.append(
                row_block(
                    [
                        metric_block(
                            "Recent Conversions",
                            recent,
                            f"Last 14 days ({status_formatted})",
                            trend,
                        ),
                        metric_block("Prior Conversions", prior, "Prior 14 days"),
                    ]
                )
            )

        blocks.append(
            badge_block(
                f"Health Score: {health_score}/100 ({health_label})", variant=variant
            )
        )

    def _append_budget_summary(
        self, blocks: list[dict], rec: CampaignRecommendation
    ) -> None:
        """Append budget static summary."""
        bb = rec.fields.budget_bidding
        if not bb:
            return

        blocks.append(divider_block())
        blocks.append(heading_block("Budget & Bidding", level=3))

        currency = bb.currency_code if hasattr(bb, "currency_code") else "INR"
        curr = (
            _format_currency(bb.current_budget, currency) if bb.current_budget else ""
        )

        items = []
        if curr:
            items.append({"key": "Current Budget", "value": curr})

        strategy_str = (bb.current_strategy or "Manual CPC").replace("_", " ").title()
        if strategy_str:
            items.append({"key": "Bidding Strategy", "value": strategy_str})

        constraint = (bb.constraint_type or "NONE").upper()
        if constraint != "NONE":
            _constraint_display = {
                "BUDGET_CONSTRAINED": "Budget Limited",
                "BID_CONSTRAINED": "Bid / Quality Score",
                "MIXED_CONSTRAINT": "Budget + Bid Mixed",
            }.get(constraint, constraint.replace("_", " ").title())
            items.append({"key": "Constraint", "value": _constraint_display})

        if items:
            blocks.append(key_value_block(items))

        no_budget_rec = bb.recommended_budget is None
        no_bidding_rec = not bb.bidding_rec_type or bb.bidding_rec_type.upper() == "NONE"
        if no_budget_rec and no_bidding_rec and constraint == "NONE":
            blocks.append(badge_block("Budget & Bidding: No issues detected", variant="success"))

        if bb.learning_phase_warning:
            blocks.append(
                callout_block(f"Learning Phase: {bb.learning_phase_warning}", "warning")
            )
        if bb.pacing_warning:
            blocks.append(callout_block(f"Pacing: {bb.pacing_warning}", "warning"))
        if bb.blocking_issues:
            blocks.append(
                callout_block(
                    f"Blocking Issues: {', '.join(bb.blocking_issues)}", "danger"
                )
            )
