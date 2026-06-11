"""Budget Diagnosis Service.

Pure diagnosis logic — no API calls. Takes a normalised campaign context dict
(output of ``CampaignMetricsAdapter``) and a bidding recommendation dict
(output of ``BiddingAdvisorService``), and produces a budget constraint
diagnosis with confidence scoring.

Responsibilities
----------------
- Classify the campaign's constraint type using impression share signals.
- Guard against IS signal misuse on Maximize Conversions campaigns (IS is
  unreliable for strategies designed to spend the full budget by design).
- Compute a recommended budget amount when the constraint is budget-driven.
- Generate a pacing warning string when the recommended change > 20%.
- Downgrade confidence when impression share metrics are stale.
- Detect MOVE_UNUSED_BUDGET opportunities (shared/excess budgets).
- Surface an advisory note when budget appears low relative to tCPA target.

What this service does NOT do
------------------------------
- It does not fetch any data from the Google Ads API.
- It does not hard-block any recommendation. All outputs are confidence-scored
  so callers (BudgetBiddingAdvisorService) can present them with appropriate
  context rather than suppressing them.
- It does NOT enforce a "daily budget ≥ 2× tCPA" rule as a hard gate.
  Google's documentation does not state this as a Smart Bidding requirement.
  The 2× figure in Google's Help Center describes overdelivery behaviour
  (Google may spend up to 2× daily budget on high-traffic days), not a
  budget-sizing rule for Smart Bidding strategies. We surface a low-budget
  advisory note only — we do not auto-raise the recommended budget based
  on this unverified multiplier.
  Source: https://support.google.com/google-ads/answer/1704424

Constraint classification
--------------------------
The three-way split is derived from the impression share identity:

    Search IS + Lost IS (Budget) + Lost IS (Rank) ≈ 100%

    BUDGET_CONSTRAINED  : budget_lost_IS > 10%, rank_lost_IS < 20%
    BID_CONSTRAINED     : rank_lost_IS  > 20%, budget_lost_IS < 5%
    MIXED_CONSTRAINT    : both signals elevated
    NONE                : neither signal elevated

All thresholds are configurable practitioner conventions, not official
Google limits. They are exposed as module-level constants so they can be
tuned per deployment without code changes.

Reference
---------
  https://developers.google.com/google-ads/api/docs/campaigns/budgets/overview
  https://support.google.com/google-ads/answer/7103314  (impression share)
  https://support.google.com/google-ads/answer/1704424  (overdelivery / 2× daily budget)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.agents.adzump.agents.optimization.models import (
    ConfidenceLevel,
    ConstraintType,
    BiddingStrategyType,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configurable thresholds  (practitioner conventions, not official Google limits)
# ---------------------------------------------------------------------------

# Impression share thresholds for constraint classification.
BUDGET_LOST_IS_THRESHOLD: float = 0.10  # > 10% impressions lost to budget
RANK_LOST_IS_THRESHOLD: float = 0.20  # > 20% impressions lost to rank

# Pacing warning threshold — surface a warning when recommended change > 20%.
# This is a UI hint, not a hard cap. No enforcement happens in this layer.
PACING_WARNING_THRESHOLD: float = 0.20

# Low-budget advisory ratio relative to tCPA target.
# When current_budget < target_cpa * this ratio, an advisory note is added
# to the recommendation rationale. This is NOT enforced as a minimum budget
# and does NOT auto-raise the recommended budget amount.
#
# NOTE: The commonly cited "2× tCPA" rule does not appear in Google's official
# documentation as a Smart Bidding budget requirement. Google's Help Center
# mentions 2× only in the context of overdelivery (daily spend may go up to
# 2× daily budget on high-traffic days). The ratio below is a conservative
# advisory signal only — tune it per account if needed.
# Source: https://support.google.com/google-ads/answer/1704424
LOW_BUDGET_ADVISORY_RATIO: float = 2.0

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _classify_constraint(
    budget_lost_is: Optional[float],
    rank_lost_is: Optional[float],
) -> ConstraintType:
    """Classify campaign constraint type from impression share signals.

    Parameters
    ----------
    budget_lost_is:
        Fraction of eligible impressions lost due to budget (0.0–1.0).
        None when unavailable or stale.
    rank_lost_is:
        Fraction of eligible impressions lost due to Ad Rank (0.0–1.0).
        None when unavailable or stale.
    """
    b = budget_lost_is or 0.0
    r = rank_lost_is or 0.0

    budget_constrained = b > BUDGET_LOST_IS_THRESHOLD
    rank_constrained = r > RANK_LOST_IS_THRESHOLD

    if budget_constrained and rank_constrained:
        return ConstraintType.MIXED_CONSTRAINT
    if budget_constrained:
        return ConstraintType.BUDGET_CONSTRAINED
    if rank_constrained:
        return ConstraintType.BID_CONSTRAINED
    return ConstraintType.NONE


def _budget_change_warning(
    current_budget: float,
    recommended_budget: float,
) -> Optional[str]:
    """Generate a pacing warning string when the change exceeds the threshold.

    This is a UI hint only — the caller surfaces it to the user. No
    programmatic enforcement occurs here.
    """
    if current_budget <= 0:
        return None
    pct_change = (recommended_budget - current_budget) / current_budget
    if pct_change > PACING_WARNING_THRESHOLD:
        return (
            f"This recommendation increases the budget by "
            f"{pct_change:.0%}. Large single-step budget changes can "
            f"temporarily disrupt pacing. Consider applying this in stages."
        )
    return None


def _low_budget_advisory(
    current_budget: float,
    target_cpa: Optional[float],
) -> Optional[str]:
    """Return an advisory note when budget looks low relative to tCPA target.

    This is a UI hint only — it does NOT raise the recommended budget amount
    and does NOT block any recommendation.

    The 2* ratio is a conservative advisory signal. Google's documentation
    does not state it as a Smart Bidding requirement.
    Source: https://support.google.com/google-ads/answer/1704424
    """
    if not target_cpa or current_budget <= 0:
        return None
    advisory_threshold = target_cpa * LOW_BUDGET_ADVISORY_RATIO
    if current_budget < advisory_threshold:
        return (
            f"Current budget ({current_budget:.2f}/day) is less than "
            f"{LOW_BUDGET_ADVISORY_RATIO:.0f}* the target CPA "
            f"({target_cpa:.2f}). Smart Bidding may have limited room to "
            f"explore auctions. Consider reviewing the budget if performance "
            f"is below expectations."
        )
    return None


# ---------------------------------------------------------------------------
# Main diagnosis function
# ---------------------------------------------------------------------------


def diagnose_budget(
    campaign_context: Dict[str, Any],
    bidding_recommendation: Optional[Dict[str, Any]] = None,
    native_budget_recs: Optional[list] = None,
) -> Dict[str, Any]:
    """Produce a budget constraint diagnosis for a single campaign.

    Parameters
    ----------
    campaign_context:
        Normalised campaign context dict from ``CampaignMetricsAdapter``.
    bidding_recommendation:
        Output dict from ``BiddingAdvisorService`` for this campaign, or
        ``None`` when no bidding change is recommended.
    native_budget_recs:
        List of parsed native recommendation dicts for this campaign from
        ``GoogleRecommendationsAdapter`` (budget rec types only), or ``None``.

    Returns
    -------
    dict
        Budget diagnosis containing:

        ``constraint_type``       — ConstraintType classification.
        ``budget_confidence``     — "high" | "medium" | "low".
        ``recommended_budget``    — Suggested daily budget in currency units,
                                    or None.
        ``budget_rec_rationale``  — Human-readable explanation.
        ``pacing_warning``        — Warning string or None.
        ``google_rec_confirmed``  — True when native rec agrees with diagnosis.
        ``blocking_issues``       — List of issue strings surfaced to the user.
        ``native_rec_amount``     — Google's suggested budget from native rec,
                                    or None.
        ``move_unused_signal``    — True when MOVE_UNUSED_BUDGET rec exists.
    """
    budget_lost_is = campaign_context.get("budget_lost_impression_share")
    rank_lost_is = campaign_context.get("rank_lost_impression_share")
    current_budget = campaign_context.get("budget_amount", 0.0)
    target_cpa = campaign_context.get("target_cpa")
    freshness = campaign_context.get("metric_freshness", {})
    campaign_name = campaign_context.get("campaign_name", "")

    blocking_issues: list[str] = []
    native_rec_amount: Optional[float] = None
    move_unused_signal = False
    google_rec_confirmed = False

    # ------------------------------------------------------------------
    # Step 1: Parse native recommendations
    # ------------------------------------------------------------------
    if native_budget_recs:
        for rec in native_budget_recs:
            rec_type = rec.get("type", "")
            details = rec.get("details", {})

            if rec_type in (
                "CAMPAIGN_BUDGET",
                "FORECASTING_CAMPAIGN_BUDGET",
                "MARGINAL_ROI_CAMPAIGN_BUDGET",
            ):
                native_rec_amount = details.get("recommended_budget")

            elif rec_type == "MOVE_UNUSED_BUDGET":
                move_unused_signal = True
                logger.info(
                    "diagnose_budget: MOVE_UNUSED_BUDGET signal detected campaign=%s",
                    campaign_name,
                )

    # ------------------------------------------------------------------
    # Step 2: Classify constraint
    #
    # IMPORTANT — Maximize Conversions IS incompatibility guard:
    # Google explicitly states that search_budget_lost_impression_share is
    # unreliable for Maximize Conversions campaigns because the strategy is
    # designed to spend the full daily budget — it is always "limited by
    # budget" by design. Using IS data to classify this strategy as
    # BUDGET_CONSTRAINED produces a false positive.
    # Source: https://support.google.com/google-ads/answer/7381968
    #
    # For Maximize Conversions and Maximize Conversion Value campaigns:
    # - Force constraint_type = "NONE" (IS signal is not meaningful)
    # - Downgrade base_confidence to "low"
    # - Add an explanatory note to blocking_issues
    # ------------------------------------------------------------------
    strategy_type = campaign_context.get("bidding_strategy_type", "")
    IS_INCOMPATIBLE_STRATEGIES = {
        BiddingStrategyType.MAXIMIZE_CONVERSIONS,
        BiddingStrategyType.MAXIMIZE_CONVERSION_VALUE,
    }

    base_confidence = ConfidenceLevel.HIGH
    constraint_type = _classify_constraint(budget_lost_is, rank_lost_is)

    if strategy_type in IS_INCOMPATIBLE_STRATEGIES:
        if constraint_type != ConstraintType.NONE:
            logger.info(
                "diagnose_budget: IS incompatible strategy=%s overriding "
                "constraint_type=%s to NONE for campaign=%s",
                strategy_type,
                constraint_type,
                campaign_name,
            )
            blocking_issues.append(
                f"This campaign uses {strategy_type}, which is designed to spend "
                "the full daily budget by design. Search impression share metrics "
                "are not a reliable signal for budget constraints under this strategy."
            )
            base_confidence = ConfidenceLevel.LOW
        constraint_type = ConstraintType.NONE

    # ------------------------------------------------------------------
    # Step 3: Apply freshness downgrade
    # ------------------------------------------------------------------
    if not freshness.get("is_fresh", True):
        base_confidence = ConfidenceLevel.LOW
        blocking_issues.append(
            freshness.get("warning", "Impression share data may be stale.")
        )
        logger.info(
            "diagnose_budget: stale metrics detected campaign=%s age_days=%s",
            campaign_name,
            freshness.get("age_days"),
        )

    # ------------------------------------------------------------------
    # Step 4: Reconcile diagnosis with native recs and produce output
    # ------------------------------------------------------------------
    recommended_budget: Optional[float] = None
    rationale: Optional[str] = None
    budget_confidence = base_confidence

    if constraint_type == ConstraintType.BUDGET_CONSTRAINED:
        if native_rec_amount:
            # Google agrees — high confidence signal
            recommended_budget = native_rec_amount
            google_rec_confirmed = True
            if base_confidence != ConfidenceLevel.LOW:
                budget_confidence = ConfidenceLevel.HIGH
            rationale = (
                f"Campaign is losing {(budget_lost_is or 0):.0%} of eligible "
                f"impressions due to budget. Google's recommendation engine "
                f"suggests increasing to {native_rec_amount:.2f}/day."
            )
        else:
            # Our diagnosis says budget-constrained but no native rec to confirm.
            # Surface with medium confidence.
            if base_confidence != ConfidenceLevel.LOW:
                budget_confidence = ConfidenceLevel.MEDIUM
            rationale = (
                f"Campaign is losing {(budget_lost_is or 0):.0%} of eligible "
                f"impressions due to budget. Consider increasing the daily budget."
            )

    elif constraint_type == ConstraintType.BID_CONSTRAINED:
        # Google may have issued a CAMPAIGN_BUDGET rec but we override it —
        # the root cause is Ad Rank, not budget exhaustion.
        if native_rec_amount:
            blocking_issues.append(
                f"Google suggests a budget increase to {native_rec_amount:.2f}/day, "
                f"but {(rank_lost_is or 0):.0%} of impressions are lost due to "
                f"Ad Rank — not budget. Fix bids or Quality Score first."
            )
        rationale = (
            f"Campaign is losing {(rank_lost_is or 0):.0%} of eligible "
            f"impressions due to Ad Rank. A budget increase will not help until "
            f"bid competitiveness or Quality Score is improved."
        )
        # No budget recommendation for BID_CONSTRAINED campaigns.
        if base_confidence != ConfidenceLevel.LOW:
            budget_confidence = ConfidenceLevel.MEDIUM

    elif constraint_type == ConstraintType.MIXED_CONSTRAINT:
        rationale = (
            f"Campaign is losing impressions to both budget "
            f"({(budget_lost_is or 0):.0%}) and Ad Rank "
            f"({(rank_lost_is or 0):.0%}). Resolve bidding issues first, "
            f"then reassess budget."
        )
        if native_rec_amount:
            blocking_issues.append(
                "Hold budget increase until Ad Rank constraint is resolved."
            )
        if base_confidence != ConfidenceLevel.LOW:
            budget_confidence = ConfidenceLevel.MEDIUM

    else:
        # NONE — campaign is not meaningfully constrained by budget or rank.
        rationale = "No significant budget or rank constraint detected."

    # ------------------------------------------------------------------
    # Step 5: Low-budget advisory note (tCPA campaigns only)
    # Advisory only — does NOT raise recommended_budget.
    # The 2× tCPA rule is not stated in Google's official documentation
    # as a Smart Bidding requirement. We surface an informational note only.
    # ------------------------------------------------------------------
    low_budget_note = _low_budget_advisory(current_budget, target_cpa)
    if low_budget_note:
        if rationale:
            rationale += f" Advisory: {low_budget_note}"
        else:
            rationale = low_budget_note
        logger.info(
            "diagnose_budget: low_budget_advisory triggered campaign=%s",
            campaign_name,
        )
    # ------------------------------------------------------------------
    # Step 6: Learning phase guard
    # ------------------------------------------------------------------
    learning_phase_warning: Optional[str] = None
    if (
        bidding_recommendation
        and bidding_recommendation.get("bidding_rec_type")
        and recommended_budget
    ):
        learning_phase_warning = (
            "A bidding strategy change is also recommended. Apply the "
            "bidding change first and allow 1–2 weeks for the learning "
            "phase to complete before increasing the budget, to avoid "
            "resetting the Smart Bidding learning cycle."
        )

    # ------------------------------------------------------------------
    # Step 7: Pacing warning
    # ------------------------------------------------------------------
    pacing_warning: Optional[str] = None
    if recommended_budget and current_budget > 0:
        pacing_warning = _budget_change_warning(current_budget, recommended_budget)

    return {
        "constraint_type": constraint_type,
        "budget_confidence": budget_confidence,
        "recommended_budget": recommended_budget,
        "budget_rec_rationale": rationale,
        "pacing_warning": pacing_warning,
        "learning_phase_warning": learning_phase_warning,
        "google_rec_confirmed": google_rec_confirmed,
        "blocking_issues": blocking_issues,
        "native_rec_amount": native_rec_amount,
        "move_unused_signal": move_unused_signal,
    }
