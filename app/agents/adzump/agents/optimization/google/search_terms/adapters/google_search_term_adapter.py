from datetime import date

from structlog import get_logger

from app.agents.adzump.adapters.google.client import google_ads_client
from app.agents.adzump.agents.optimization.google.google_dateutils import (
    format_date_range,
)

logger = get_logger(__name__)


def build_metrics(metrics: dict) -> dict:
    return {
        "impressions": metrics.get("impressions", 0),
        "clicks": metrics.get("clicks", 0),
        "ctr": metrics.get("ctr", 0),
        "average_cpc": metrics.get("averageCpc", 0),
        "cost": metrics.get("costMicros", 0),
        "conversions": metrics.get("conversions", 0),
        "cost_per_conversion": metrics.get("costPerConversion", 0),
    }


class GoogleSearchTermAdapter:
    DEFAULT_DURATION = "LAST_30_DAYS"

    def __init__(self):
        self.client = google_ads_client

    async def fetch_search_terms(
        self,
        account_id: str,
        parent_account_id: str,
        client_code: str,
        auth_headers: dict[str, str],
        duration: str | None = None,
    ) -> list[dict]:

        duration_clause = format_date_range(duration or self.DEFAULT_DURATION)

        query = f"""
        SELECT
            campaign.id,
            campaign.name,
            campaign.advertising_channel_type,
            ad_group.id,
            ad_group.name,
            search_term_view.search_term,
            search_term_view.status,
            segments.search_term_match_type,
            metrics.impressions,
            metrics.clicks,
            metrics.ctr,
            metrics.average_cpc,
            metrics.cost_micros,
            metrics.conversions,
            metrics.cost_per_conversion
        FROM search_term_view
        WHERE
            {duration_clause}
            AND search_term_view.status IN ('NONE')
            AND ad_group.status = 'ENABLED'
            AND campaign.status = 'ENABLED'
        """

        try:
            results = await self.client.search_stream(
                query=query,
                customer_id=account_id,
                login_customer_id=parent_account_id,
                client_code=client_code,
                auth_headers=auth_headers,
            )
            transformed = self._transform_results(results, account_id, parent_account_id)
            logger.info(
                "search_terms_fetched", account_id=account_id, total=len(transformed)
            )
            return transformed

        except Exception as e:
            logger.warning(
                "failed_to_fetch_search_terms", account_id=account_id, error=str(e)
            )
            return []

    def _transform_results(
        self, results: list, account_id: str, parent_account_id: str
    ) -> list[dict]:
        """
        Transform raw Google Ads API response
        into normalized internal structure.
        """

        transformed: list[dict] = []

        for entry in results:
            campaign = entry.get("campaign", {})
            ad_group = entry.get("adGroup", {})
            search_term_view = entry.get("searchTermView", {})

            search_term = search_term_view.get("searchTerm")

            if not search_term:
                continue

            transformed.append(
                {
                    "account_id": account_id,
                    "parent_account_id": parent_account_id,
                    "campaign_id": str(campaign.get("id")),
                    "campaign_name": campaign.get("name"),
                    "campaign_type": campaign.get("advertisingChannelType"),
                    "ad_group_id": str(ad_group.get("id")),
                    "ad_group_name": ad_group.get("name"),
                    "search_term": search_term,
                    "status": search_term_view.get("status"),
                    "match_type": (
                        entry.get("segments", {}).get("searchTermMatchType")
                    ),
                    "metrics": build_metrics(entry.get("metrics", {})),
                }
            )

        return transformed
