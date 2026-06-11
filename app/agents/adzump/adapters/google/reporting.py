"""Google Ads Reporting Adapter.

Fetches keyword performance metrics (impressions, clicks, conversions, cost, etc.)
via the Google Ads API (GAQL). Returns flat dicts in the canonical shape consumed
by MetricPerformanceEvaluator and all downstream advisors.

The evaluator and entire downstream pipeline expects: entry["metrics"]["impressions"].

Reference
---------
  https://developers.google.com/google-ads/api/fields/v24/campaign (campaign.end_date — "2037-12-30" is the API sentinel for perpetual/no-end-date campaigns, not NULL)
"""

import logging
from datetime import date
from typing import Optional

from app.agents.adzump.adapters.google.client import google_ads_client
from app.agents.adzump.adapters.google.conversion_enums import QueryWindow

logger = logging.getLogger(__name__)


def _micros_to_currency(micros: int | float) -> float:
    """Convert Google Ads micros to currency units."""
    return round(float(micros) / 1_000_000, 2) if micros else 0.0


def _build_metrics(raw: dict) -> dict:
    """Build the nested "metrics" dict from a raw Google Ads API row.

    Canonical shape expected by MetricPerformanceEvaluator and all downstream consumers.
    """
    clicks = int(raw.get("clicks", 0))
    conversions = float(raw.get("conversions", 0))
    cost = _micros_to_currency(raw.get("costMicros", 0))
    cpl_raw = float(raw.get("costPerConversion", 0))

    return {
        "impressions": int(raw.get("impressions", 0)),
        "clicks": clicks,
        "conversions": conversions,
        "cost": cost,
        "ctr": round(float(raw.get("ctr", 0)) * 100, 2),
        "average_cpc": _micros_to_currency(raw.get("averageCpc", 0)),
        "cpl": _micros_to_currency(cpl_raw) if cpl_raw > 0 else None,
        "conv_rate": round(conversions / clicks * 100, 2) if clicks > 0 else None,
    }


class GoogleReportingAdapter:
    DEFAULT_DURATION = QueryWindow.LAST_30_DAYS.value

    def __init__(self):
        self.client = google_ads_client

    async def fetch_keyword_metrics(
        self,
        account_id: str,
        parent_account_id: str,
        client_code: str,
        auth_headers: dict,
        campaign_ids: Optional[list[str]] = None,
    ) -> list[dict]:
        """Fetch keyword performance metrics for a Google Ads account.

        If campaign_ids is provided, the GAQL query will be filtered to only fetch
        metrics for those specific campaigns.
        Returns a list of plain dicts in the canonical shape:
        {"keyword": ..., "campaign_id": ..., "metrics": {"impressions": ..., ...}, ...}
        """
        query = self._build_keyword_query(
            duration=self.DEFAULT_DURATION, include_metrics=True, campaign_ids=campaign_ids
        )

        results = await self.client.search_stream(
            query=query,
            customer_id=account_id,
            login_customer_id=parent_account_id,
            client_code=client_code,
            auth_headers=auth_headers,
        )

        keywords = [_transform_row(row) for row in results]
        logger.info(
            "keyword_metrics_fetched account_id=%s count=%d", account_id, len(keywords)
        )
        return keywords

    def _build_keyword_query(
        self,
        campaign_id: Optional[str] = None,
        ad_group_id: Optional[str] = None,
        duration: Optional[str] = None,
        include_negatives: bool = False,
        include_metrics: bool = False,
        campaign_ids: Optional[list[str]] = None,
    ) -> str:
        date_clause = f"segments.date DURING {duration}" if duration else ""
        ad_group_filter = f"AND ad_group.id = {ad_group_id}" if ad_group_id else ""
        
        campaign_filter = ""
        if campaign_ids:
            ids_str = ", ".join(f"'{cid}'" for cid in campaign_ids)
            campaign_filter = f"AND campaign.id IN ({ids_str})"
        elif campaign_id:
            campaign_filter = f"AND campaign.id = {campaign_id}"

        negative_filter = (
            "" if include_negatives else "AND ad_group_criterion.negative = FALSE"
        )

        metrics_fields = ""
        if include_metrics:
            metrics_fields = """,
                   metrics.impressions, metrics.clicks, metrics.conversions,
                   metrics.cost_micros, metrics.ctr, metrics.average_cpc,
                   metrics.cost_per_conversion"""

        return f"""
            SELECT campaign.id, campaign.name, campaign.advertising_channel_type,
                   ad_group.id, ad_group.name,
                   ad_group_criterion.criterion_id,
                   ad_group_criterion.resource_name,
                   ad_group_criterion.status,
                   ad_group_criterion.keyword.text,
                   ad_group_criterion.keyword.match_type,
                   ad_group_criterion.quality_info.quality_score{metrics_fields}
            FROM keyword_view
            WHERE campaign.status = 'ENABLED'
                AND campaign.end_date >= '{date.today().strftime("%Y-%m-%d")}'
                AND ad_group.status = 'ENABLED'
                AND ad_group_criterion.status = 'ENABLED'
                {campaign_filter}
                {ad_group_filter}
                {"AND " + date_clause if date_clause else ""}
                {negative_filter}
            ORDER BY ad_group.id, ad_group_criterion.criterion_id
        """.strip()


def _transform_row(row: dict) -> dict:
    """Parse a Google Ads API response row into the canonical keyword dict shape.

    Returns the exact shape expected by MetricPerformanceEvaluator:
    entry["metrics"]["impressions"], entry["quality_score"], etc.
    """
    campaign = row.get("campaign", {})
    ad_group = row.get("adGroup", {})
    criterion = row.get("adGroupCriterion", {})
    keyword_info = criterion.get("keyword", {})

    return {
        "keyword": keyword_info.get("text", "").strip().lower(),
        "criterion_id": str(criterion.get("criterionId", "")),
        "resource_name": criterion.get("resourceName", ""),
        "match_type": keyword_info.get("matchType", "PHRASE"),
        "ad_group_id": str(ad_group.get("id", "")),
        "ad_group_name": ad_group.get("name", ""),
        "campaign_id": str(campaign.get("id", "")),
        "campaign_name": campaign.get("name", ""),
        "campaign_type": campaign.get("advertisingChannelType", "SEARCH"),
        "status": criterion.get("status", "UNKNOWN"),
        "is_negative": criterion.get("negative", False),
        "quality_score": criterion.get("qualityInfo", {}).get("qualityScore"),
        "metrics": _build_metrics(row.get("metrics", {})),
    }


google_reporting_adapter = GoogleReportingAdapter()
