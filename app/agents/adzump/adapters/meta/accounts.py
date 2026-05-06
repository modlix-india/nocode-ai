"""Meta accounts adapter — lists business, ad, page and IG assets via Graph API.

Ported from ``ds/adapters/meta/accounts.py``. Auth headers are passed per-call.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.adzump.adapters.meta.client import meta_client

logger = logging.getLogger(__name__)


class MetaAccountsAdapter:
    async def list_business_accounts(
        self, client_code: str, auth_headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        result = await meta_client.get(
            "/me/businesses",
            client_code=client_code,
            auth_headers=auth_headers,
            params={"fields": "id,name"},
        )
        return [
            {"id": b["id"], "name": b.get("name", b["id"])}
            for b in result.get("data", [])
        ]

    async def list_ad_accounts(
        self,
        business_id: str,
        client_code: str,
        auth_headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        result = await meta_client.get(
            f"/{business_id}/owned_ad_accounts",
            client_code=client_code,
            auth_headers=auth_headers,
            params={"fields": "id,name,account_status"},
        )
        return [
            {"id": a["id"], "name": a.get("name", a["id"])}
            for a in result.get("data", [])
            if a.get("account_status") == 1
        ]

    async def list_fb_pages(
        self,
        business_id: str,
        client_code: str,
        auth_headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        """FB pages accessible to the business — client_pages first, else owned_pages."""
        result = await meta_client.get(
            f"/{business_id}/client_pages",
            client_code=client_code,
            auth_headers=auth_headers,
            params={"fields": "id,name"},
        )
        pages = result.get("data", [])

        if not pages:
            result = await meta_client.get(
                f"/{business_id}/owned_pages",
                client_code=client_code,
                auth_headers=auth_headers,
                params={"fields": "id,name"},
            )
            pages = result.get("data", [])

        return [{"id": p["id"], "name": p.get("name", p["id"])} for p in pages]

    async def list_ig_accounts(
        self,
        page_id: str,
        client_code: str,
        auth_headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        """IG Business accounts linked to THIS FB page only.

        Diverges from ds PR #273 on purpose: ds adds a business-level fallback
        that returns ALL IG accounts under the business when the page has no
        linked IG, which silently attaches unrelated accounts to the campaign.
        We return [] and let the tool layer tell the user to link an IG account
        to the page or pick a different page.
        """
        # 1. Find the page access token via /me/accounts.
        pages_data = await meta_client.get(
            "/me/accounts",
            client_code=client_code,
            auth_headers=auth_headers,
            params={"fields": "id,access_token", "limit": 100},
        )
        page_token = next(
            (p.get("access_token") for p in pages_data.get("data", [])
             if str(p.get("id")) == str(page_id)),
            None,
        )
        # Fallback: ask for the token directly on the page.
        if not page_token:
            try:
                page_info = await meta_client.get(
                    f"/{page_id}",
                    client_code=client_code,
                    auth_headers=auth_headers,
                    params={"fields": "access_token"},
                )
                page_token = page_info.get("access_token")
            except Exception:
                pass
        if not page_token:
            raise RuntimeError(
                f"Could not find a Page Access Token for page {page_id}. "
                "Ensure the connected Meta user has manage_pages permission."
            )

        # 2. Try the connection edge (uses page access token, not user token).
        conn = await meta_client.get(
            f"/{page_id}/instagram_accounts",
            client_code=client_code,
            auth_headers=auth_headers,
            params={"fields": "id,username"},
            access_token=page_token,
        )
        ig_accounts = conn.get("data", [])

        tier1_count = len(ig_accounts)

        # 3. Fallback: instagram_business_account field on the page.
        tier2_hit = False
        if not ig_accounts:
            field_res = await meta_client.get(
                f"/{page_id}",
                client_code=client_code,
                auth_headers=auth_headers,
                params={"fields": "instagram_business_account{id,username}"},
                access_token=page_token,
            )
            biz_acc = field_res.get("instagram_business_account")
            if biz_acc:
                ig_accounts = [biz_acc]
                tier2_hit = True

        if not ig_accounts:
            logger.warning(
                "meta_ig_no_page_linked: page=%s tier1=%d tier2=%s",
                page_id, tier1_count, "hit" if tier2_hit else "miss",
            )

        return [
            {"id": str(a["id"]), "name": a.get("username", str(a["id"]))}
            for a in ig_accounts
        ]
