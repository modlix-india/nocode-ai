from __future__ import annotations
import logging

from app.core.session import AuthContext
from app.agents.adzump.agents.optimization.platform_handlers import PlatformHandler
from app.agents.adzump.adapters.google.accounts import GoogleAccountsAdapter
from app.agents.adzump.adapters.google.client import google_ads_client
from app.agents.adzump.agents.optimization.agent import get_optimization_agent
from app.agents.adzump.agents.optimization.models import (
    CampaignOverview,
    CampaignStatus,
)

logger = logging.getLogger(__name__)


class GooglePlatformHandler(PlatformHandler):
    """PlatformHandler implementation for Google.

    Uses `GoogleAccountsAdapter` and the existing Google Ads client for
    campaign discovery, and delegates analysis to `OptimizationAgent`.
    """

    def __init__(self) -> None:
        self.accounts_adapter = GoogleAccountsAdapter()

    async def fetch_accessible_accounts(
        self, client_code: str, auth_headers: dict
    ) -> list[dict]:
        return await self.accounts_adapter.fetch_accessible_accounts(
            client_code, auth_headers=auth_headers
        )

    async def find_campaign_ids_in_account(
        self,
        account_id: str,
        login_customer_id: str,
        client_code: str,
        auth_headers: dict,
        campaign_ids: list[str],
    ) -> set[str]:
        if not campaign_ids:
            return set()

        found: set[str] = set()
        chunk_size = 100
        for start in range(0, len(campaign_ids), chunk_size):
            chunk = [
                cid.strip()
                for cid in campaign_ids[start : start + chunk_size]
                if cid and str(cid).strip().isdigit()
            ]
            if not chunk:
                continue
            id_list = ", ".join(f"'{cid}'" for cid in chunk)
            query = (
                "SELECT campaign.id, campaign.name "
                "FROM campaign "
                f"WHERE campaign.id IN ({id_list})"
            )
            try:
                rows = await google_ads_client.search(
                    query=query,
                    customer_id=account_id,
                    login_customer_id=login_customer_id,
                    client_code=client_code,
                    auth_headers=auth_headers,
                )
            except Exception:
                logger.warning(
                    "platform_handlers.google: campaign lookup failed account=%s",
                    account_id,
                    exc_info=True,
                )
                continue

            for row in rows:
                campaign_id = str(row.get("campaign", {}).get("id", ""))
                if campaign_id:
                    found.add(campaign_id)

        logger.info(
            "platform_handlers.google: account=%s matched_mapped_campaigns=%d",
            account_id,
            len(found),
        )
        return found

    async def _fetch_raw_campaign_data(
        self,
        campaign_id: str,
        account_id: str,
        login_customer_id: str,
        client_code: str,
        auth_headers: dict,
    ) -> tuple[dict | None, int, int]:
        """Fetch raw campaign context and counts concurrently to avoid duplicate API calls."""
        from app.agents.adzump.adapters.google.campaign_metrics import (
            campaign_metrics_adapter,
        )
        import asyncio

        ctx_task = campaign_metrics_adapter.fetch_campaign_contexts(
            customer_id=account_id,
            login_customer_id=login_customer_id,
            client_code=client_code,
            auth_headers=auth_headers,
            campaign_ids=[campaign_id],
        )
        counts_task = campaign_metrics_adapter.fetch_campaign_counts(
            customer_id=account_id,
            login_customer_id=login_customer_id,
            client_code=client_code,
            auth_headers=auth_headers,
            campaign_ids=[campaign_id],
        )

        results = await asyncio.gather(ctx_task, counts_task, return_exceptions=True)
        ctx_list = results[0]
        counts_map = results[1]

        if isinstance(ctx_list, Exception):
            logger.error(
                "GooglePlatformHandler: failed to fetch campaign context: %s",
                ctx_list,
                exc_info=ctx_list,
            )
            ctx_list = None
        if isinstance(counts_map, Exception):
            logger.error(
                "GooglePlatformHandler: failed to fetch campaign counts: %s",
                counts_map,
                exc_info=counts_map,
            )
            counts_map = {}

        ctx_data = ctx_list[0] if ctx_list else None
        counts = counts_map.get(campaign_id, {"adset_count": 0, "ad_count": 0})

        return ctx_data, counts.get("adset_count", 0), counts.get("ad_count", 0)

    async def run_campaign_headless(
        self,
        campaign_id: str,
        account_id: str,
        login_customer_id: str,
        client_code: str,
        auth_headers: dict,
        mapping: dict,
        auth: AuthContext = None,
    ):
        ctx_data, adset_count, ad_count = await self._fetch_raw_campaign_data(
            campaign_id=campaign_id,
            account_id=account_id,
            login_customer_id=login_customer_id,
            client_code=client_code,
            auth_headers=auth_headers,
        )

        overview = None
        campaign_name_from_api = campaign_id
        if ctx_data:
            campaign_name_from_api = ctx_data.get("campaign_name", campaign_id)
            status_str = ctx_data.get("campaign_status", CampaignStatus.UNKNOWN)
            try:
                status = CampaignStatus(status_str)
            except ValueError:
                status = CampaignStatus.UNKNOWN

            overview = CampaignOverview(
                status=status,
                currency_code=ctx_data.get("currency_code", "INR"),
                spend=ctx_data.get("cost", 0.0),
                conversions=ctx_data.get("conversions", 0.0),
                adset_count=adset_count,
                ad_count=ad_count,
                campaign_name=campaign_name_from_api,
            )

        agent = get_optimization_agent()
        # Delegate to the existing headless runner which is already platform-aware
        return await agent.run_headless(
            campaign_id=campaign_id,
            account_id=account_id,
            login_customer_id=login_customer_id,
            client_code=client_code,
            auth_headers=auth_headers,
            platform="GOOGLE",
            product_id=mapping.get("product_id", ""),
            product_name=mapping.get("product_name", ""),
            campaign_name=campaign_name_from_api
            if "campaign_name_from_api" in locals()
            else mapping.get("name", campaign_id),
            overview=overview,
            auth=auth,
        )

    async def fetch_campaign_overview(
        self,
        campaign_id: str,
        account_id: str,
        login_customer_id: str,
        client_code: str,
        auth_headers: dict,
    ) -> CampaignOverview | None:
        ctx_data, adset_count, ad_count = await self._fetch_raw_campaign_data(
            campaign_id=campaign_id,
            account_id=account_id,
            login_customer_id=login_customer_id,
            client_code=client_code,
            auth_headers=auth_headers,
        )
        if not ctx_data:
            return None

        status_str = ctx_data.get("campaign_status", CampaignStatus.UNKNOWN)
        try:
            status = CampaignStatus(status_str)
        except ValueError:
            status = CampaignStatus.UNKNOWN

        return CampaignOverview(
            status=status,
            currency_code=ctx_data.get("currency_code", "INR"),
            spend=ctx_data.get("cost", 0.0),
            conversions=ctx_data.get("conversions", 0.0),
            adset_count=adset_count,
            ad_count=ad_count,
            campaign_name=ctx_data.get("campaign_name"),
        )
