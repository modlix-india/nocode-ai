"""Conversion Metrics Adapter.

Data-collection layer for all conversion-related GAQL queries.
Both conversion_signal and verify_conversion_health import from here
they own only their business logic.

Reference
---------
  https://developers.google.com/google-ads/api/reference/rpc/v23/ConversionAction
  https://developers.google.com/google-ads/api/reference/rpc/v23/Customer
  https://developers.google.com/google-ads/api/docs/conversions/overview
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from typing import Any, Dict, List, Optional

from app.agents.adzump.adapters.google.client import google_ads_client
from app.agents.adzump.adapters.google.gaql import safe_id
from app.agents.adzump.adapters.google.conversion_enums import (
    AttributionModel,
    ConversionActionCategory,
    ConversionActionCountingType,
    ConversionActionStatus,
    ConversionActionType,
    ConversionTrackingStatus,
    WEBSITE_TAG_TYPES,
)
from app.agents.adzump.adapters.google.recommendations import (
    google_recommendations_adapter,
    RecommendationType,
)

logger = logging.getLogger(__name__)


# Date range helper
def date_range_28d() -> tuple[str, str]:
    """Return (start_date, end_date) ISO strings for the last 28 days.

    Computes an explicit BETWEEN range (yesterday and 27 days prior)
    since LAST_28_DAYS is not a valid GAQL DURING enum value.
    """
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=27)
    return start.isoformat(), end.isoformat()


# Conversion signal query — uses explicit BETWEEN date range.
# {start_date} and {end_date} are filled at call time via date_range_28d().
# {campaign_id} is filled at call time.
# We fetch the full 28-day window in one call and split client-side into
# two 14-day halves. This avoids two round trips.
_CONVERSION_SIGNAL_QUERY = """
SELECT
  campaign.id,
  campaign.name,
  campaign.status,
  campaign.bidding_strategy_type,
  campaign.start_date,
  metrics.conversions,
  segments.date
FROM campaign
WHERE
  campaign.id = '{campaign_id}'
  AND campaign.status = 'ENABLED'
  AND segments.date BETWEEN '{start_date}' AND '{end_date}'
ORDER BY segments.date ASC
"""

# Customer tracking status — no date segment needed.
_CUSTOMER_TRACKING_QUERY = """
SELECT
  customer.conversion_tracking_setting.conversion_tracking_status,
  customer.conversion_tracking_setting.google_ads_conversion_customer
FROM customer
"""

# Conversion actions — no date segment (actions are account-level resources).
# Excludes REMOVED actions.
_CONVERSION_ACTION_QUERY = """
SELECT
  conversion_action.id,
  conversion_action.name,
  conversion_action.resource_name,
  conversion_action.status,
  conversion_action.type,
  conversion_action.category,
  conversion_action.tag_snippets,
  conversion_action.counting_type,
  conversion_action.primary_for_goal,
  conversion_action.include_in_conversions_metric,
  conversion_action.value_settings.always_use_default_value,
  conversion_action.value_settings.default_value,
  conversion_action.attribution_model_settings.attribution_model,
  conversion_action.attribution_model_settings.data_driven_model_status,
  conversion_action.click_through_lookback_window_days,
  conversion_action.view_through_lookback_window_days
FROM conversion_action
WHERE conversion_action.status != 'REMOVED'
"""

# Campaign conversion goals — scoped to a specific campaign.
# {campaign_id} filled at call time.
_CONVERSION_GOAL_QUERY = """
SELECT
  campaign_conversion_goal.resource_name,
  campaign_conversion_goal.campaign,
  campaign_conversion_goal.category,
  campaign_conversion_goal.origin,
  campaign_conversion_goal.biddable
FROM campaign_conversion_goal
WHERE campaign.id = '{campaign_id}'
"""


# Row parsers
def _parse_tracking_status(tracking_rows: List[dict]) -> str:
    """Extract ConversionTrackingStatus string from customer query rows."""
    if not tracking_rows:
        return ConversionTrackingStatus.UNKNOWN.value
    ct = tracking_rows[0].get("customer", {}).get("conversionTrackingSetting", {})
    return ct.get("conversionTrackingStatus", ConversionTrackingStatus.UNKNOWN.value)


def _parse_signal_rows(
    rows_raw: List[dict],
) -> tuple[List[dict], str | None, str]:
    """Parse campaign rows from the conversion signal query into flat dicts, start date, and bidding strategy."""
    flat_rows: List[dict] = []
    campaign_start_date: Optional[str] = None
    bidding_strategy: str = ""

    for row in rows_raw:
        camp = row.get("campaign", {})
        metrics = row.get("metrics", {})
        segments = row.get("segments", {})

        if not campaign_start_date:
            campaign_start_date = camp.get("startDate")
        if not bidding_strategy:
            bidding_strategy = camp.get("biddingStrategyType", "")

        flat_rows.append(
            {
                "date": segments.get("date", ""),
                "metrics": {
                    "conversions": float(metrics.get("conversions", 0)),
                },
            }
        )

    return flat_rows, campaign_start_date, bidding_strategy


def _parse_conversion_actions(actions_rows: List[dict]) -> List[dict]:
    """Parse conversion_action query rows into normalized action dicts with raw API string values."""
    actions = []
    for row in actions_rows:
        ca = row.get("conversionAction", {})
        vs = ca.get("valueSettings", {})
        am = ca.get("attributionModelSettings", {})
        actions.append(
            {
                "id": str(ca.get("id", "")),
                "name": ca.get("name", ""),
                # Mutation target for auto-apply (e.g. attribution / counting fixes).
                "resource_name": ca.get("resourceName", ""),
                "status": ca.get("status", ConversionActionStatus.UNKNOWN.value),
                "type": ca.get("type", ConversionActionType.UNKNOWN.value),
                "category": ca.get("category", ConversionActionCategory.UNKNOWN.value),
                "counting_type": ca.get(
                    "countingType", ConversionActionCountingType.UNKNOWN.value
                ),
                "primary_for_goal": bool(ca.get("primaryForGoal", False)),
                "include_in_conversions_metric": bool(
                    ca.get("includeInConversionsMetric", False)
                ),
                "tag_snippets": ca.get("tagSnippets", []),
                "attribution_model": am.get(
                    "attributionModel", AttributionModel.UNKNOWN.value
                ),
                "data_driven_model_status": am.get("dataDrivenModelStatus", "UNKNOWN"),
                "always_use_default_value": bool(
                    vs.get("alwaysUseDefaultValue", False)
                ),
                "default_value": float(vs.get("defaultValue", 0.0)),
                "click_through_lookback_window_days": ca.get(
                    "clickThroughLookbackWindowDays"
                ),
                "view_through_lookback_window_days": ca.get(
                    "viewThroughLookbackWindowDays"
                ),
                "requires_tag": ca.get("type", "") in WEBSITE_TAG_TYPES,
            }
        )
    return actions


def _parse_conversion_goals(goal_rows: List[dict], campaign_id: str) -> List[dict]:
    """Parse campaign_conversion_goal query rows."""
    goals = []
    for row in goal_rows:
        ccg = row.get("campaignConversionGoal", {})
        goals.append(
            {
                "campaign_id": campaign_id,
                # Mutation target for the biddable auto-apply.
                "resource_name": ccg.get("resourceName", ""),
                "category": ccg.get("category", ConversionActionCategory.UNKNOWN.value),
                "origin": ccg.get("origin", "UNKNOWN"),
                "biddable": bool(ccg.get("biddable", False)),
            }
        )
    return goals


# Conversion Metrics Adapter


class ConversionMetricsAdapter:
    """Data-collection layer for conversion-related GAQL queries containing no business logic."""

    def __init__(self) -> None:
        self.client = google_ads_client

    async def fetch_conversion_signal(
        self,
        campaign_id: str,
        customer_id: str,
        login_customer_id: str,
        client_code: str,
        auth_headers: dict,
    ) -> Dict[str, Any]:
        """Fetch campaign metrics (28-day window) and customer tracking status in parallel.

        Returns a dictionary containing flat rows, campaign start date, bidding strategy,
        tracking status, and the date range.
        """
        start_date, end_date = date_range_28d()
        signal_query = _CONVERSION_SIGNAL_QUERY.format(
            campaign_id=safe_id(campaign_id, "campaign_id"),
            start_date=start_date,
            end_date=end_date,
        )

        logger.info(
            "fetch_conversion_signal: campaign=%s range=%s to %s customer=%s",
            campaign_id,
            start_date,
            end_date,
            customer_id,
        )

        rows_raw, tracking_rows = await asyncio.gather(
            self.client.search(
                query=signal_query,
                customer_id=customer_id,
                login_customer_id=login_customer_id,
                client_code=client_code,
                auth_headers=auth_headers,
            ),
            self.client.search(
                query=_CUSTOMER_TRACKING_QUERY,
                customer_id=customer_id,
                login_customer_id=login_customer_id,
                client_code=client_code,
                auth_headers=auth_headers,
            ),
        )

        flat_rows, campaign_start_date, bidding_strategy = _parse_signal_rows(rows_raw)
        tracking_status = _parse_tracking_status(tracking_rows)

        logger.info(
            "fetch_conversion_signal_done: campaign=%s rows=%d tracking=%s",
            campaign_id,
            len(flat_rows),
            tracking_status,
        )

        return {
            "flat_rows": flat_rows,
            "campaign_start_date": campaign_start_date,
            "bidding_strategy": bidding_strategy,
            "tracking_status": tracking_status,
            "date_range": (start_date, end_date),
        }

    async def fetch_conversion_health(
        self,
        campaign_id: str,
        customer_id: str,
        login_customer_id: str,
        client_code: str,
        auth_headers: dict,
    ) -> Dict[str, Any]:
        """Fetch active conversion actions, customer tracking status, and campaign goals in parallel."""
        goal_query = _CONVERSION_GOAL_QUERY.format(
            campaign_id=safe_id(campaign_id, "campaign_id")
        )

        logger.info(
            "fetch_conversion_health: campaign=%s customer=%s",
            campaign_id,
            customer_id,
        )

        # Nested to avoid passing customer_id/auth_headers on every call
        async def _search(query: str) -> List[dict]:
            try:
                return await self.client.search(
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
                        "fetch_conversion_health._search auth error customer=%s: %s",
                        customer_id, err_str[:300],
                    )
                    raise
                logger.exception(
                    "fetch_conversion_health._search failed: customer=%s",
                    customer_id,
                )
                return []

        (
            actions_rows,
            tracking_rows,
            goal_rows,
            tag_coverage_recs,
        ) = await asyncio.gather(
            _search(_CONVERSION_ACTION_QUERY),
            _search(_CUSTOMER_TRACKING_QUERY),
            _search(goal_query),
            google_recommendations_adapter.fetch_recommendations(
                customer_id=customer_id,
                login_customer_id=login_customer_id,
                client_code=client_code,
                auth_headers=auth_headers,
                recommendation_type=RecommendationType.IMPROVE_GOOGLE_TAG_COVERAGE,
            ),
        )

        actions = _parse_conversion_actions(actions_rows)
        tracking_status = _parse_tracking_status(tracking_rows)
        goals = _parse_conversion_goals(goal_rows, campaign_id)

        logger.info(
            "fetch_conversion_health_done: campaign=%s actions=%d goals=%d tracking=%s tag_coverage=%d",
            campaign_id,
            len(actions),
            len(goals),
            tracking_status,
            len(tag_coverage_recs),
        )

        return {
            "actions": actions,
            "tracking_status": tracking_status,
            "goals": goals,
            "tag_coverage_recs": tag_coverage_recs,
        }


# Singleton

conversion_metrics_adapter = ConversionMetricsAdapter()
