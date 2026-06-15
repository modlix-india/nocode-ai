"""Budget & Bidding Advisor Service.

Runs the full diagnosis pipeline for campaign budget and bidding.

1. Fetch campaign context
2. Fetch native Google bidding recommendations
3. Fetch native Google budget recommendations
4. Score bidding strategy changes
5. Diagnose budget constraints
6. Merge results into BudgetBiddingRecommendation

Key points:
- Bidding recommendations are scored first, then budget diagnosis uses that
  result for learning-phase and tCPA minimum budget logic.
- Portfolio campaigns keep target changes at the portfolio level.
- Conversion volume affects confidence, not recommendation eligibility.
- Stale impression share lowers budget confidence.
- Google native recommendations confirm or augment our diagnosis.
"""

# Sources:
# - https://developers.google.com/google-ads/api/docs/campaigns/bidding/strategy-types
# - https://developers.google.com/google-ads/api/docs/recommendations
# - https://support.google.com/google-ads/answer/6268632  (tCPA page — no switch threshold)
# - https://support.google.com/google-ads/answer/7381968  (Max Conv — no minimum stated)
# - https://support.google.com/google-ads/answer/6268637  (tROAS — no Search minimum)
# - https://support.google.com/google-ads/faq/10286469  (Target ROAS guidance)

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from app.agents.adzump.adapters.google.campaign_metrics import (
    campaign_metrics_adapter,
)
from app.agents.adzump.adapters.google.recommendations import (
    RecommendationType,
    google_recommendations_adapter,
)
from app.agents.adzump.recommendations.google.advisors.budget.diagnosis_service import (
    diagnose_budget,
)
from app.agents.adzump.agents.optimization.models import (
    BiddingStrategyType,
    BudgetBiddingRecommendation,
    ConfidenceLevel,
    ConstraintType,
    ScopeType,
)
from app.agents.adzump._shared import build_ds_headers

logger = logging.getLogger(__name__)

# Recommendation type groups

_BIDDING_REC_TYPES: List[RecommendationType] = [
    RecommendationType.TARGET_CPA_OPT_IN,
    RecommendationType.TARGET_ROAS_OPT_IN,
    RecommendationType.MAXIMIZE_CONVERSIONS_OPT_IN,
    RecommendationType.MAXIMIZE_CONVERSION_VALUE_OPT_IN,
    RecommendationType.MAXIMIZE_CLICKS_OPT_IN,
    RecommendationType.RAISE_TARGET_CPA,
    RecommendationType.LOWER_TARGET_ROAS,
    RecommendationType.SET_TARGET_CPA,
    RecommendationType.SET_TARGET_ROAS,
]

_BUDGET_REC_TYPES: List[RecommendationType] = [
    RecommendationType.CAMPAIGN_BUDGET,
    RecommendationType.FORECASTING_CAMPAIGN_BUDGET,
    RecommendationType.MARGINAL_ROI_CAMPAIGN_BUDGET,
    RecommendationType.MOVE_UNUSED_BUDGET,
]


# Strategy maturity thresholds are practitioner conventions, not Google policy.
# Google does not publish Search bidding switch minimums, so these values are
# used as a decision-tree starting point rather than a hard rule.
# The sources are listed above; the 30-conversion note is measurement guidance,
# not an explicit bidding strategy switch threshold.
# Minimum conversions/month before recommending TARGET_CPA_OPT_IN from MANUAL_CPC.
# Practitioner convention — not from Google documentation.
CONV_THRESHOLD_TARGET_CPA: int = 30

# Minimum conversions/month before recommending MAXIMIZE_CONVERSIONS as a
# stepping stone toward tCPA. Practitioner convention — not from Google docs.
CONV_THRESHOLD_MAX_CONVERSIONS: int = 15

# Minimum conversions/month before recommending TARGET_ROAS_OPT_IN from TARGET_CPA.
# The 50 figure appears in Google's Smart Bidding FAQ only as measurement guidance:
# "50 conversions for Target ROAS" — not as a switching threshold.
# Source: https://support.google.com/google-ads/faq/10286469
CONV_THRESHOLD_TARGET_ROAS: int = 50

# Upper bound below which tROAS target is considered too restrictive.
# Practitioner convention — not from Google documentation.
CONV_THRESHOLD_ROAS_LOW_VOLUME: int = 20

_CONV_THRESHOLDS: Dict[str, Dict[str, int]] = {
    RecommendationType.TARGET_ROAS_OPT_IN: {
        "high": CONV_THRESHOLD_TARGET_ROAS,
        "medium": CONV_THRESHOLD_MAX_CONVERSIONS,
    },
    RecommendationType.SET_TARGET_ROAS: {
        "high": CONV_THRESHOLD_TARGET_ROAS,
        "medium": CONV_THRESHOLD_MAX_CONVERSIONS,
    },
    RecommendationType.TARGET_CPA_OPT_IN: {
        "high": CONV_THRESHOLD_TARGET_CPA,
        "medium": CONV_THRESHOLD_MAX_CONVERSIONS,
    },
    RecommendationType.SET_TARGET_CPA: {
        "high": CONV_THRESHOLD_TARGET_CPA,
        "medium": CONV_THRESHOLD_MAX_CONVERSIONS,
    },
    RecommendationType.RAISE_TARGET_CPA: {
        "high": CONV_THRESHOLD_TARGET_CPA,
        "medium": CONV_THRESHOLD_MAX_CONVERSIONS,
    },
    RecommendationType.MAXIMIZE_CONVERSIONS_OPT_IN: {
        "high": CONV_THRESHOLD_MAX_CONVERSIONS,
        "medium": CONV_THRESHOLD_MAX_CONVERSIONS // 2,
    },
    RecommendationType.MAXIMIZE_CONVERSION_VALUE_OPT_IN: {
        "high": CONV_THRESHOLD_MAX_CONVERSIONS,
        "medium": CONV_THRESHOLD_MAX_CONVERSIONS // 2,
    },
    RecommendationType.MAXIMIZE_CLICKS_OPT_IN: {"high": 0, "medium": 0},
    RecommendationType.LOWER_TARGET_ROAS: {
        "high": CONV_THRESHOLD_TARGET_ROAS,
        "medium": CONV_THRESHOLD_MAX_CONVERSIONS,
    },
}


def _conversion_confidence(
    conversions: float,
    rec_type: RecommendationType,
) -> ConfidenceLevel:
    """Score bidding recommendation confidence from conversion volume.

    Returns ConfidenceLevel — never blocks the recommendation.
    """
    thresholds = _CONV_THRESHOLDS.get(rec_type, {"high": 0, "medium": 0})
    if conversions >= thresholds["high"]:
        return ConfidenceLevel.HIGH
    if conversions >= thresholds["medium"]:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


# Bidding advisor logic (internal helper for bidding recommendations)


def _score_bidding_recommendation(
    campaign_context: Dict[str, Any],
    native_bidding_recs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Score bidding recommendations using native and internal signals.

    - Use Google's native recommendation when available.
    - Otherwise apply a strategy maturity decision tree.
    - Portfolio target changes are marked with PORTFOLIO scope.

    Returns a dict with keys:
        bidding_rec_type, bidding_rec_rationale,
        bidding_confidence, bidding_blocked_reason,
        google_rec_confirmed.
    """
    conversions = campaign_context.get("conversions", 0.0)
    strategy_type = campaign_context.get("bidding_strategy_type", "")
    is_portfolio = campaign_context.get("is_portfolio", False)
    target_roas = campaign_context.get("target_roas")

    # Map native recs by type for O(1) lookup
    native_by_type: Dict[str, Dict[str, Any]] = {}
    for r in native_bidding_recs:
        t = r.get("type", "")
        # Handle string and enum keys safely
        t_str = t.value if isinstance(t, RecommendationType) else str(t)
        native_by_type[t_str] = r

    # Priority 1: Native Google recommendation present
    # Iterate in priority order — more specific/advanced strategies first.
    priority_order = [
        RecommendationType.TARGET_ROAS_OPT_IN,
        RecommendationType.TARGET_CPA_OPT_IN,
        RecommendationType.MAXIMIZE_CONVERSION_VALUE_OPT_IN,
        RecommendationType.MAXIMIZE_CONVERSIONS_OPT_IN,
        RecommendationType.RAISE_TARGET_CPA,
        RecommendationType.LOWER_TARGET_ROAS,
        RecommendationType.SET_TARGET_CPA,
        RecommendationType.SET_TARGET_ROAS,
        RecommendationType.MAXIMIZE_CLICKS_OPT_IN,
    ]
    for rec_type in priority_order:
        if rec_type.value in native_by_type:
            confidence = _conversion_confidence(conversions, rec_type)
            rationale = (
                f"Google's recommendation engine suggests switching to "
                f"{rec_type.value.replace('_', ' ').title()}. "
            )
            if confidence == ConfidenceLevel.LOW:
                rationale += (
                    f"Note: conversion volume ({conversions:.0f}/month) is "
                    f"below the recommended threshold for reliable "
                    f"Smart Bidding performance."
                )
            return {
                "bidding_rec_type": rec_type.value,
                "bidding_rec_rationale": rationale,
                "bidding_confidence": confidence,
                "bidding_blocked_reason": None,
                "google_rec_confirmed": True,
            }

    # Priority 2: No native rec — apply strategy maturity decision tree.
    # Thresholds are practitioner conventions defined above.

    rec_type: Optional[RecommendationType] = None
    rationale = ""

    if strategy_type == BiddingStrategyType.MANUAL_CPC:
        if conversions >= CONV_THRESHOLD_TARGET_CPA:
            rec_type = RecommendationType.TARGET_CPA_OPT_IN
            rationale = (
                f"Campaign has stable conversion volume ({conversions:.0f}/month) on Manual CPC. "
                "Switching to Target CPA will help automate bidding and improve cost efficiency."
            )
        elif conversions >= CONV_THRESHOLD_MAX_CONVERSIONS:
            rec_type = RecommendationType.MAXIMIZE_CONVERSIONS_OPT_IN
            rationale = (
                f"Campaign has moderate conversion volume ({conversions:.0f}/month). "
                "Switching to Maximize Conversions will help build consistent conversion data."
            )
        else:
            rec_type = RecommendationType.MAXIMIZE_CLICKS_OPT_IN
            rationale = (
                f"Campaign has low conversion volume ({conversions:.0f}/month). "
                "Switching to Maximize Clicks will help drive traffic and build initial conversion data."
            )

    elif strategy_type == BiddingStrategyType.MAXIMIZE_CONVERSIONS:
        # Maximize Conversions is designed to spend full budget, so
        # impression share budget-constraint signals are unreliable.
        # Treat this strategy's budget diagnosis as low confidence.
        # Source: https://support.google.com/google-ads/answer/7381968
        if conversions >= CONV_THRESHOLD_TARGET_CPA:
            rec_type = RecommendationType.TARGET_CPA_OPT_IN
            rationale = (
                f"Campaign has sufficient conversion volume ({conversions:.0f}/month) on Maximize Conversions. "
                "Adding a Target CPA constraint will help manage cost per conversion more effectively."
            )

    elif strategy_type == BiddingStrategyType.TARGET_CPA:
        if conversions >= CONV_THRESHOLD_TARGET_ROAS and not target_roas:
            rec_type = RecommendationType.TARGET_ROAS_OPT_IN
            rationale = (
                f"Campaign has high conversion volume ({conversions:.0f}/month) on Target CPA. "
                "If conversion values are tracked, switching to Target ROAS will optimize for overall revenue rather than conversion volume."
            )

    elif strategy_type == BiddingStrategyType.TARGET_ROAS:
        if conversions < CONV_THRESHOLD_ROAS_LOW_VOLUME and target_roas:
            rec_type = RecommendationType.LOWER_TARGET_ROAS
            rationale = (
                f"Campaign has low conversion volume ({conversions:.0f}/month) for a Target ROAS strategy. "
                "Lowering the ROAS target slightly will help relax strict auction filters and unlock more volume."
            )

    if not rec_type:
        return {
            "bidding_rec_type": None,
            "bidding_rec_rationale": "No bidding strategy change recommended.",
            "bidding_confidence": ConfidenceLevel.HIGH,
            "bidding_blocked_reason": None,
            "google_rec_confirmed": False,
        }

    confidence = _conversion_confidence(conversions, rec_type)

    # Portfolio scope note — target changes must be made at portfolio level
    if is_portfolio and rec_type.value in (
        RecommendationType.RAISE_TARGET_CPA.value,
        RecommendationType.LOWER_TARGET_ROAS.value,
        RecommendationType.SET_TARGET_CPA.value,
        RecommendationType.SET_TARGET_ROAS.value,
        RecommendationType.TARGET_CPA_OPT_IN.value,
        RecommendationType.TARGET_ROAS_OPT_IN.value,
    ):
        rationale += (
            " This campaign uses a portfolio bidding strategy — apply the "
            "target change at the portfolio level to avoid disrupting other "
            "campaigns sharing the same strategy."
        )

    return {
        "bidding_rec_type": rec_type.value,
        "bidding_rec_rationale": rationale,
        "bidding_confidence": confidence,
        "bidding_blocked_reason": None,
        "google_rec_confirmed": False,
    }


# Main orchestrator


class BudgetBiddingAdvisorService:
    """Orchestrates the Budget & Bidding Diagnosis Engine pipeline."""

    def __init__(self) -> None:
        self.metrics_adapter = campaign_metrics_adapter
        self.recs_adapter = google_recommendations_adapter

    async def analyse(
        self,
        account_id: str,
        parent_id: str,
        client_code: str,
        context: Dict[str, Any],
        campaign_ids: Optional[List[str]] = None,
    ) -> List[BudgetBiddingRecommendation]:
        """Run the full diagnosis pipeline and return merged recommendations."""
        auth_headers = build_ds_headers(context)
        common = dict(
            customer_id=account_id,
            login_customer_id=parent_id,
            client_code=client_code,
            auth_headers=auth_headers,
        )

        # Fetch all data sources concurrently

        logger.info(
            "budget_bidding_advisor.analyse: account=%s campaigns=%s",
            account_id,
            campaign_ids or "ALL",
        )

        campaign_contexts_task = self.metrics_adapter.fetch_campaign_contexts(
            **common, campaign_ids=campaign_ids
        )

        native_bidding_recs_task = self.recs_adapter.fetch_multiple_types(
            **common, recommendation_types=_BIDDING_REC_TYPES
        )

        native_budget_recs_task = self.recs_adapter.fetch_multiple_types(
            **common, recommendation_types=_BUDGET_REC_TYPES
        )

        campaign_contexts, native_bidding_map, native_budget_map = await asyncio.gather(
            campaign_contexts_task,
            native_bidding_recs_task,
            native_budget_recs_task,
        )

        if not campaign_contexts:
            logger.warning(
                "budget_bidding_advisor.analyse: no campaign contexts returned "
                "for account=%s",
                account_id,
            )
            return []

        # Index native recs by campaign resource name for fast lookup.
        def _index_by_campaign(
            recs_map: Dict[str, List[Dict[str, Any]]],
        ) -> Dict[str, List[Dict[str, Any]]]:
            """Flatten {rec_type: [recs]} → {campaign_resource: [recs]}."""
            indexed: Dict[str, List[Dict[str, Any]]] = {}
            for recs in recs_map.values():
                for rec in recs:
                    camp_resource = rec.get("campaign", "")
                    if camp_resource:
                        indexed.setdefault(camp_resource, []).append(rec)
            return indexed

        bidding_recs_by_campaign = _index_by_campaign(native_bidding_map)
        budget_recs_by_campaign = _index_by_campaign(native_budget_map)

        # Score bidding → diagnose budget → merge per campaign

        results: List[BudgetBiddingRecommendation] = []

        for ctx in campaign_contexts:
            campaign_id = ctx["campaign_id"]
            # Build the campaign resource name used by Google recommendation records.
            camp_resource = f"customers/{account_id}/campaigns/{campaign_id}"

            native_bidding_recs = bidding_recs_by_campaign.get(camp_resource, [])
            native_budget_recs = budget_recs_by_campaign.get(camp_resource, [])

            # Bidding advisor
            bidding_result = _score_bidding_recommendation(ctx, native_bidding_recs)

            # Budget diagnosis (receives bidding result for learning guard)
            budget_result = diagnose_budget(
                campaign_context=ctx,
                bidding_recommendation=bidding_result,
                native_budget_recs=native_budget_recs,
            )

            # Determine apply order
            apply_order: List[str] = []
            if bidding_result.get("bidding_rec_type"):
                apply_order.append("bidding")
            if budget_result.get("recommended_budget"):
                apply_order.append("budget")
            # Scope determination
            scope = ScopeType.PORTFOLIO if ctx["is_portfolio"] else ScopeType.CAMPAIGN

            # Merge into model
            rec = BudgetBiddingRecommendation(
                campaign_id=campaign_id,
                campaign_name=ctx["campaign_name"],
                scope=scope,
                portfolio_strategy_id=ctx.get("portfolio_strategy_id"),
                portfolio_strategy_name=ctx.get("portfolio_strategy_name"),
                current_strategy=ctx["bidding_strategy_type"],
                bidding_rec_type=bidding_result.get("bidding_rec_type"),
                bidding_rec_rationale=bidding_result.get("bidding_rec_rationale"),
                bidding_confidence=bidding_result.get(
                    "bidding_confidence", ConfidenceLevel.LOW
                ),
                bidding_blocked_reason=bidding_result.get("bidding_blocked_reason"),
                current_budget=ctx["budget_amount"],
                recommended_budget=budget_result.get("recommended_budget"),
                budget_rec_type=(
                    RecommendationType.CAMPAIGN_BUDGET.value
                    if budget_result.get("recommended_budget")
                    else None
                ),
                budget_rec_rationale=budget_result.get("budget_rec_rationale"),
                budget_confidence=budget_result.get(
                    "budget_confidence", ConfidenceLevel.LOW
                ),
                apply_order=apply_order,
                learning_phase_warning=budget_result.get("learning_phase_warning"),
                constraint_type=budget_result.get(
                    "constraint_type", ConstraintType.NONE
                ),
                google_rec_confirmed=(
                    bidding_result.get("google_rec_confirmed", False)
                    or budget_result.get("google_rec_confirmed", False)
                ),
                blocking_issues=budget_result.get("blocking_issues", []),
                pacing_warning=budget_result.get("pacing_warning"),
                move_unused_budget_signal=budget_result.get(
                    "move_unused_signal", False
                ),
                metric_freshness_warning=(
                    ctx.get("metric_freshness", {}).get("warning")
                ),
            )
            results.append(rec)

        logger.info(
            "budget_bidding_advisor.analyse_done: account=%s recommendations=%d",
            account_id,
            len(results),
        )
        return results


# Singleton

budget_bidding_advisor = BudgetBiddingAdvisorService()
