"""Campaign Metrics Adapter.

Data-collection layer for the Budget & Bidding Diagnosis Engine.
Fetches campaign performance, bidding configuration, and portfolio strategy details
in a single GAQL query, normalizing output for downstream advisors.

Reference
---------
  https://developers.google.com/google-ads/api/docs/campaigns/bidding/assign-strategies
  https://developers.google.com/google-ads/api/docs/campaigns/budgets/overview
  https://developers.google.com/google-ads/api/docs/query/overview
  https://developers.google.com/google-ads/scripts/docs/solutions/adsmanagerapp-bid-to-impression-share (formatImpressionShare)
  https://developers.google.com/google-ads/api/docs/reporting/zero-metrics
  https://developers.google.com/google-ads/api/fields/v22/metrics (field definitions for all campaign metrics)
  https://developers.google.com/google-ads/api/docs/reporting/segmentation (GAQL segmentation — which segments are compatible with which metrics)
  https://groups.google.com/g/adwords-api/c/QvkmIcnDF3M/m/Vu5uFcESDAAJ (search_impression_share returns per-day values when segments.date is used; correct aggregation is impression-weighted average)
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from app.agents.adzump.adapters.google.client import google_ads_client
from app.agents.adzump.adapters.google.conversion_enums import (
    MAX_FRESHNESS_DAYS,
    QueryWindow,
)

logger = logging.getLogger(__name__)

# GAQL query

_CAMPAIGN_METRICS_QUERY = """
SELECT
  customer.currency_code,
  campaign.id,
  campaign.name,
  campaign.status,
  campaign.bidding_strategy,
  campaign.bidding_strategy_type,
  campaign.target_cpa.target_cpa_micros,
  campaign.maximize_conversions.target_cpa_micros,
  campaign.target_roas.target_roas,
  campaign.maximize_conversion_value.target_roas,
  campaign_budget.amount_micros,
  campaign_budget.explicitly_shared,
  bidding_strategy.id,
  bidding_strategy.name,
  bidding_strategy.type,
  bidding_strategy.target_cpa.target_cpa_micros,
  bidding_strategy.target_roas.target_roas,
  bidding_strategy.status,
  metrics.cost_micros,
  metrics.conversions,
  metrics.conversions_value,
  metrics.clicks,
  metrics.impressions,
  metrics.search_impression_share,
  metrics.search_budget_lost_impression_share,
  metrics.search_rank_lost_impression_share,
  segments.date
FROM campaign
WHERE campaign.status = 'ENABLED'
  AND segments.date DURING {window}
"""


# Freshness assessment


def _assess_metric_freshness(last_segment_date: Optional[str]) -> Dict[str, Any]:
    """Assess metric freshness based on the 24-48 hour computation lag of impression share data."""
    if not last_segment_date:
        return {
            "is_fresh": False,
            "age_days": None,
            "warning": (
                "Impression share data age is unknown. "
                "Constraint diagnosis confidence is low."
            ),
        }

    try:
        segment_dt = datetime.strptime(last_segment_date, "%Y-%m-%d").date()
    except ValueError:
        logger.warning(
            "Unrecognised date format for freshness check: %s",
            last_segment_date,
        )
        return {
            "is_fresh": False,
            "age_days": None,
            "warning": "Could not parse segment date.",
        }

    delta = (date.today() - segment_dt).days
    is_fresh = delta <= MAX_FRESHNESS_DAYS
    return {
        "is_fresh": is_fresh,
        "age_days": delta,
        "warning": (
            f"Impression share data is {delta} day(s) old. "
            "Constraint diagnosis may not reflect the current day's spend."
        )
        if not is_fresh
        else None,
    }


# Portfolio detection helper


def _is_portfolio(campaign_row: dict) -> bool:
    """Return True if the campaign uses a portfolio bidding strategy."""
    return bool(campaign_row.get("campaign", {}).get("biddingStrategy"))


# Target extraction helpers


def _extract_target_cpa(campaign: dict) -> Optional[float]:
    """Extract the effective target CPA in currency units from standard or maximize_conversions strategies."""
    micros = campaign.get("targetCpa", {}).get("targetCpaMicros") or campaign.get(
        "maximizeConversions", {}
    ).get("targetCpaMicros")
    return float(micros) / 1_000_000 if micros else None


def _extract_target_roas(campaign: dict) -> Optional[float]:
    """Extract the effective target ROAS ratio from standard or maximize_conversion_value strategies."""
    return campaign.get("targetRoas", {}).get("targetRoas") or campaign.get(
        "maximizeConversionValue", {}
    ).get("targetRoas")


def _normalise_row(row: dict, freshness: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a raw GoogleAdsRow dict into the normalised campaign context schema."""
    campaign = row.get("campaign", {})
    budget = row.get("campaignBudget", {})
    metrics = row.get("metrics", {})
    bs = row.get("biddingStrategy", {})  # populated for portfolio campaigns only

    portfolio = _is_portfolio(row)

    # Resolve effective tCPA and tROAS — prefer portfolio-level values when
    # the campaign is on a portfolio strategy, fall back to campaign-level.
    if portfolio and bs:
        target_cpa = (
            float(bs.get("targetCpa", {}).get("targetCpaMicros", 0)) / 1_000_000 or None
        )
        target_roas = bs.get("targetRoas", {}).get("targetRoas") or None
        portfolio_status = bs.get("status")
    else:
        target_cpa = _extract_target_cpa(campaign)
        target_roas = _extract_target_roas(campaign)
        portfolio_status = None

    budget_micros = budget.get("amountMicros")

    return {
        # Campaign identity
        "campaign_id": str(campaign.get("id", "")),
        "campaign_name": campaign.get("name", ""),
        "campaign_status": campaign.get("status", ""),
        "currency_code": row.get("customer", {}).get("currencyCode", "INR"),
        # Bidding
        "bidding_strategy_type": campaign.get("biddingStrategyType", ""),
        "target_cpa": target_cpa,
        "target_roas": target_roas,
        # Portfolio
        "is_portfolio": portfolio,
        "portfolio_strategy_resource": campaign.get("biddingStrategy", ""),
        "portfolio_strategy_id": str(bs.get("id", "")) if portfolio else None,
        "portfolio_strategy_name": bs.get("name") if portfolio else None,
        "portfolio_strategy_type": bs.get("type") if portfolio else None,
        "portfolio_strategy_status": portfolio_status,
        # Budget
        "budget_amount": float(budget_micros) / 1_000_000 if budget_micros else 0.0,
        "budget_explicitly_shared": budget.get("explicitlyShared", False),
        # Performance (30-day aggregates)
        "cost": float(metrics.get("costMicros", 0)) / 1_000_000,
        "conversions": float(metrics.get("conversions", 0)),
        "conversions_value": float(metrics.get("conversionsValue", 0)),
        "clicks": int(metrics.get("clicks", 0)),
        # Impression share (may be stale — see freshness gate)
        "search_impression_share": metrics.get("searchImpressionShare"),
        "budget_lost_impression_share": metrics.get("searchBudgetLostImpressionShare"),
        "rank_lost_impression_share": metrics.get("searchRankLostImpressionShare"),
        # Freshness metadata — attached so advisors can downgrade confidence
        "metric_freshness": freshness,
    }


class CampaignMetricsAdapter:
    """Fetches and normalizes campaign contexts for the Budget & Bidding Engine."""

    def __init__(self) -> None:
        self.client = google_ads_client

    async def fetch_campaign_contexts(
        self,
        customer_id: str,
        login_customer_id: str,
        client_code: str,
        auth_headers: dict,
        campaign_ids: Optional[List[str]] = None,
        window: QueryWindow = QueryWindow.LAST_30_DAYS,
    ) -> List[Dict[str, Any]]:
        """Fetch and aggregate campaign performance, bidding, and freshness metrics from Google Ads."""
        query = _CAMPAIGN_METRICS_QUERY.format(window=window.value)
        if campaign_ids:
            id_list = ", ".join(f"'{cid}'" for cid in campaign_ids)
            query += f"\n  AND campaign.id IN ({id_list})"

        logger.info(
            "fetch_campaign_contexts: customer=%s campaign_filter=%s",
            customer_id,
            campaign_ids or "ALL",
        )

        try:
            results = await self.client.search(
                query=query,
                customer_id=customer_id,
                login_customer_id=login_customer_id,
                client_code=client_code,
                auth_headers=auth_headers,
            )
        except Exception as exc:
            err_str = str(exc)
            if any(k in err_str for k in ("401", "403", "UNAUTHENTICATED", "PERMISSION_DENIED")):
                logger.error(
                    "fetch_campaign_contexts auth error customer=%s: %s",
                    customer_id, err_str[:300],
                )
                raise
            logger.exception("fetch_campaign_contexts failed: customer=%s", customer_id)
            return []

        # Determine the most recent segment date across all rows for freshness gate.
        # LAST_30_DAYS returns one row per day; we aggregate across all days.
        segment_dates: List[str] = []
        rows_by_campaign: Dict[str, dict] = {}

        for row in results:
            seg_date = row.get("segments", {}).get("date")
            if seg_date:
                segment_dates.append(seg_date)

            # Aggregate metrics across the 30-day window per campaign.
            # The GAQL query returns one row per (campaign, date) pair because
            # segments.date is included. We sum metrics across dates.
            cid = str(row.get("campaign", {}).get("id", ""))
            if not cid:
                continue

            if cid not in rows_by_campaign:
                # Store a copy so we don't mutate the original API response dict.
                rows_by_campaign[cid] = dict(row)
                rows_by_campaign[cid]["_metrics_agg"] = {
                    "costMicros": 0,
                    "conversions": 0.0,
                    "conversionsValue": 0.0,
                    "clicks": 0,
                    "impressions": 0,
                    "_wtd_is_sum": 0.0,
                    "_wtd_budget_lost_sum": 0.0,
                    "_wtd_rank_lost_sum": 0.0,
                }
            else:
                # Always update campaign/budget config from the latest row so we
                # don't hold stale values from the first day of the window.
                rows_by_campaign[cid]["campaign"] = row.get("campaign", {})
                rows_by_campaign[cid]["campaignBudget"] = row.get("campaignBudget", {})
                rows_by_campaign[cid]["biddingStrategy"] = row.get("biddingStrategy", {})
            agg = rows_by_campaign[cid]["_metrics_agg"]
            m = row.get("metrics", {})
            agg["costMicros"] += int(m.get("costMicros", 0))
            agg["conversions"] += float(m.get("conversions", 0))
            agg["conversionsValue"] += float(m.get("conversionsValue", 0))
            agg["clicks"] += int(m.get("clicks", 0))

            # Impression share: accumulate impression-weighted sums so the final
            # value is a true weighted average across the query window, not the
            # last row's daily figure (segments.date gives one row per day).
            impressions_for_day = int(m.get("impressions", 0))
            agg["impressions"] += impressions_for_day
            if (v := m.get("searchImpressionShare")) is not None:
                agg["_wtd_is_sum"] += impressions_for_day * float(v)
            if (v := m.get("searchBudgetLostImpressionShare")) is not None:
                agg["_wtd_budget_lost_sum"] += impressions_for_day * float(v)
            if (v := m.get("searchRankLostImpressionShare")) is not None:
                agg["_wtd_rank_lost_sum"] += impressions_for_day * float(v)

        last_segment_date = max(segment_dates) if segment_dates else None
        freshness = _assess_metric_freshness(last_segment_date)

        normalised: List[Dict[str, Any]] = []
        for cid, row in rows_by_campaign.items():
            row["metrics"] = row.pop("_metrics_agg", row.get("metrics", {}))
            agg = row["metrics"]
            total_imp = agg.pop("impressions", 0)
            wtd_is = agg.pop("_wtd_is_sum", None)
            wtd_bl = agg.pop("_wtd_budget_lost_sum", None)
            wtd_rl = agg.pop("_wtd_rank_lost_sum", None)
            if total_imp > 0:
                agg["searchImpressionShare"] = wtd_is / total_imp if wtd_is is not None else None
                agg["searchBudgetLostImpressionShare"] = wtd_bl / total_imp if wtd_bl is not None else None
                agg["searchRankLostImpressionShare"] = wtd_rl / total_imp if wtd_rl is not None else None
            else:
                agg["searchImpressionShare"] = None
                agg["searchBudgetLostImpressionShare"] = None
                agg["searchRankLostImpressionShare"] = None
            ctx = _normalise_row(row, freshness)
            normalised.append(ctx)

        logger.info(
            "fetch_campaign_contexts_done: customer=%s campaigns=%d fresh=%s",
            customer_id,
            len(normalised),
            freshness["is_fresh"],
        )
        return normalised

    async def fetch_campaign_counts(
        self,
        customer_id: str,
        login_customer_id: str,
        client_code: str,
        auth_headers: dict,
        campaign_ids: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, int]]:
        """Fetch total ad group and ad counts for the given campaigns.
        Returns: { campaign_id: {"adset_count": int, "ad_count": int} }
        """
        counts = {}
        if not campaign_ids:
            return counts
            
        # Initialize counts
        for cid in campaign_ids:
            counts[str(cid)] = {"adset_count": 0, "ad_count": 0}

        id_list = ", ".join(f"'{cid}'" for cid in campaign_ids)
        
        # We can fetch this from the ad_group_ad resource.
        # It's lighter than fetching full metrics. We just need the IDs to count unique ones.
        query = f"""
            SELECT campaign.id, ad_group.id, ad_group_ad.ad.id
            FROM ad_group_ad
            WHERE campaign.id IN ({id_list})
              AND ad_group.status = 'ENABLED'
              AND ad_group_ad.status = 'ENABLED'
        """
        
        try:
            results = await self.client.search(
                query=query,
                customer_id=customer_id,
                login_customer_id=login_customer_id,
                client_code=client_code,
                auth_headers=auth_headers,
            )
            
            # Count unique ad groups and total ads
            unique_adsets: Dict[str, set] = {cid: set() for cid in campaign_ids}
            ad_counts: Dict[str, int] = {cid: 0 for cid in campaign_ids}
            
            for row in results:
                cid = str(row.get("campaign", {}).get("id", ""))
                agid = str(row.get("adGroup", {}).get("id", ""))
                if cid in unique_adsets and agid:
                    unique_adsets[cid].add(agid)
                    ad_counts[cid] += 1
                    
            for cid in campaign_ids:
                counts[cid] = {
                    "adset_count": len(unique_adsets[cid]),
                    "ad_count": ad_counts[cid],
                }
                
        except Exception:
            logger.exception("fetch_campaign_counts failed: customer=%s", customer_id)
            
        return counts


# Singleton

campaign_metrics_adapter = CampaignMetricsAdapter()
