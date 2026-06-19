"""Google Ads accounts adapter — lists accessible MCCs and their sub-accounts.

Ported from ``ds/adapters/google/accounts.py``. Auth headers (the user's
forwarded request headers) are passed per-call instead of read from a global
ContextVar.
"""

from __future__ import annotations

import asyncio
import logging

from app.agents.adzump.adapters.google.client import google_ads_client

logger = logging.getLogger(__name__)


class GoogleAccountsAdapter:
    SUB_ACCOUNTS_QUERY = """
    SELECT customer_client.id, customer_client.descriptive_name, customer_client.manager
    FROM customer_client
    WHERE customer_client.status = 'ENABLED'
        AND customer_client.manager = FALSE
        AND customer_client.level = 1
    """

    def __init__(self) -> None:
        self.client = google_ads_client

    async def list_top_level_accounts(
        self, client_code: str, auth_headers: dict[str, str],
    ) -> list[dict]:
        """List the top-level accessible accounts (no manager expansion).

        Each entry has ``customer_id``, ``name``, ``is_manager``. Used for the
        account-selection step: the user picks a manager, then we fetch its
        children separately.
        """
        customer_ids = await self._list_customer_ids(client_code, auth_headers)
        accounts: list[dict] = []
        for cid in customer_ids:
            is_mgr, acct_name = await self._fetch_account_info(cid, client_code, auth_headers)
            accounts.append(
                {
                    "customer_id": cid,
                    "name": acct_name,
                    "is_manager": is_mgr,
                }
            )
        logger.info(
            "google_top_level_accounts: client_code=%s count=%d managers=%d",
            client_code, len(accounts), sum(1 for a in accounts if a["is_manager"]),
        )
        return accounts

    async def fetch_accessible_accounts(
        self, client_code: str, auth_headers: dict[str, str],
    ) -> list[dict]:
        """List every ad account the user can reach (managers expanded to children).

        Kept for callers that want the fully-flattened view.
        """
        top_level = await self.list_top_level_accounts(client_code, auth_headers)

        all_accounts: list[dict] = []
        seen_ids: set[str] = set()

        managers = [(e["customer_id"], e["name"]) for e in top_level if e["is_manager"]]
        non_managers = [(e["customer_id"], e["name"]) for e in top_level if not e["is_manager"]]

        if managers:
            sub_results = await asyncio.gather(
                *[self._list_sub_accounts(cid, client_code, auth_headers) for cid, _ in managers],
                return_exceptions=True,
            )
            for (cid, acct_name), result in zip(managers, sub_results):
                if isinstance(result, Exception):
                    logger.warning(
                        "google_sub_accounts_failed manager=%s error=%s", cid, result
                    )
                    continue
                for acc in result:
                    if acc["customer_id"] not in seen_ids:
                        seen_ids.add(acc["customer_id"])
                        all_accounts.append(
                            {
                                "customer_id": acc["customer_id"],
                                "name": acc.get("name", ""),
                                "login_customer_id": cid,
                                "login_customer_name": acct_name,
                                "is_manager": True,
                            }
                        )

        for cid, acct_name in non_managers:
            if cid not in seen_ids:
                seen_ids.add(cid)
                all_accounts.append(
                    {
                        "customer_id": cid,
                        "name": acct_name,
                        "login_customer_id": cid,
                        "login_customer_name": acct_name,
                        "is_manager": False,
                    }
                )

        logger.info(
            "google_accounts_fetched: client_code=%s count=%d",
            client_code, len(all_accounts),
        )
        return all_accounts

    async def _list_customer_ids(
        self, client_code: str, auth_headers: dict[str, str],
    ) -> list[str]:
        data = await self.client.get(
            "customers:listAccessibleCustomers", client_code, auth_headers,
        )
        resource_names = data.get("resourceNames", [])
        return [name.split("/")[-1] for name in resource_names]

    async def _fetch_account_info(
        self, customer_id: str, client_code: str, auth_headers: dict[str, str],
    ) -> tuple[bool, str]:
        """Return (is_manager, descriptive_name) for an account."""
        try:
            results = await self.client.search_stream(
                query="SELECT customer.manager, customer.descriptive_name FROM customer",
                customer_id=customer_id,
                login_customer_id=customer_id,
                client_code=client_code,
                auth_headers=auth_headers,
            )
            if not results:
                return False, ""
            customer = results[0].get("customer", {})
            return bool(customer.get("manager", False)), customer.get("descriptiveName", "")
        except Exception as e:
            err_str = str(e)
            if any(k in err_str for k in ("401", "403", "UNAUTHENTICATED", "PERMISSION_DENIED")):
                logger.error(
                    "google_account_info_auth_error: customer_id=%s err=%s",
                    customer_id, err_str[:300],
                )
                raise
            logger.warning("google_account_info_failed: customer_id=%s err=%s",
                           customer_id, err_str[:200])
            return False, ""

    async def _list_sub_accounts(
        self, manager_id: str, client_code: str, auth_headers: dict[str, str],
    ) -> list[dict]:
        results = await self.client.search_stream(
            query=self.SUB_ACCOUNTS_QUERY,
            customer_id=manager_id,
            login_customer_id=manager_id,
            client_code=client_code,
            auth_headers=auth_headers,
        )

        accounts: list[dict] = []
        for result in results:
            client_info = result.get("customerClient", {})
            accounts.append(
                {
                    "customer_id": str(client_info.get("id")),
                    "name": client_info.get("descriptiveName", ""),
                }
            )

        logger.info("google_sub_accounts_fetched: manager_id=%s count=%d",
                    manager_id, len(accounts))
        return accounts
