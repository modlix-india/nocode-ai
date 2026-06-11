from __future__ import annotations
import logging

from app.core.session import AuthContext
from app.agents.adzump.agents.optimization.platform_handlers import PlatformHandler
from app.agents.adzump.adapters.meta.accounts import MetaAccountsAdapter
from app.agents.adzump.adapters.meta.client import meta_client
from app.agents.adzump.agents.optimization.agent import get_optimization_agent
from app.agents.adzump.agents.optimization.models import CampaignOverview

logger = logging.getLogger(__name__)


def _clean_meta_act_id(account_id: str) -> str:
    raw = str(account_id).strip()
    return raw if raw.startswith("act_") else f"act_{raw}"


class MetaPlatformHandler(PlatformHandler):
    """Meta platform support for scheduler account discovery and campaign validation."""

    def __init__(self) -> None:
        self.accounts_adapter = MetaAccountsAdapter()

    async def fetch_accessible_accounts(
        self, client_code: str, auth_headers: dict
    ) -> list[dict]:
        businesses = await self.accounts_adapter.list_business_accounts(
            client_code, auth_headers=auth_headers
        )
        accounts: list[dict] = []
        for business in businesses:
            business_id = business.get("id")
            if not business_id:
                continue
            ad_accounts = await self.accounts_adapter.list_ad_accounts(
                business_id=business_id,
                client_code=client_code,
                auth_headers=auth_headers,
            )
            for account in ad_accounts:
                account_id = account.get("id")
                if not account_id:
                    continue
                accounts.append(
                    {
                        "customer_id": account_id,
                        "login_customer_id": business_id,
                        "name": account.get("name", account_id),
                    }
                )
        return accounts

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
        params = {"fields": "id,name", "limit": 500}
        url = f"/{_clean_meta_act_id(account_id)}/campaigns"
        try:
            while url:
                result = await meta_client.get(
                    url,
                    client_code=client_code,
                    auth_headers=auth_headers,
                    params=params,
                )
                for campaign in result.get("data", []):
                    camp_id = str(campaign.get("id", ""))
                    if camp_id and camp_id in campaign_ids:
                        found.add(camp_id)

                # Check for next page
                paging = result.get("paging", {})
                cursors = paging.get("cursors", {})
                after = cursors.get("after")
                if after:
                    params["after"] = after
                    # Keep same URL, just update params
                else:
                    break
        except Exception:
            logger.warning(
                "platform_handlers.meta: campaign lookup failed account=%s",
                account_id,
                exc_info=True,
            )
            return found

        logger.info(
            "platform_handlers.meta: account=%s matched_mapped_campaigns=%d",
            account_id,
            len(found),
        )
        return found

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
        agent = get_optimization_agent()
        return await agent.run_headless(
            campaign_id=campaign_id,
            account_id=account_id,
            login_customer_id=login_customer_id,
            client_code=client_code,
            auth_headers=auth_headers,
            platform="META",
            product_id=mapping.get("product_id", ""),
            product_name=mapping.get("product_name", ""),
            campaign_name=mapping.get("name", campaign_id),
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
        # TODO: Implement Meta overview fetch
        return None
