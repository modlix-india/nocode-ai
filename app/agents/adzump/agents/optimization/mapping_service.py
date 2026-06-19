"""Campaign → product mapping read for the optimization layer.

Reads AISuggestedData into the campaign-to-product map the resolver and tools
use. Lives here (not in services/) so its ``platform_registry`` dependency is a
plain top-level import — no ``services → optimization`` edge.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.adzump.agents.optimization.platform_registry import normalize_platform
from app.agents.adzump.services.business_storage import APP_CODE, STORAGE_NAME
from app.agents.adzump.services.storage_base import storage_service
from app.agents.adzump.services.storage_models import StorageReadRequest

logger = logging.getLogger(__name__)

# One storage page must cover all of a client's launched campaigns (no pagination
# loop here); 5000 is the safe upper bound on campaigns per client.
_MAPPING_PAGE_SIZE = 5000


async def fetch_campaign_mappings(
    client_code: str, auth_headers: dict
) -> dict[str, dict[str, Any]]:
    """Build a ``{campaign_id: {product_id, platform, account_id,
    login_customer_id, ...}}`` map from AISuggestedData.

    Cross-platform safety: ``campaign.accounts`` holds the launch platform's
    accounts; when a linked campaign is on a different platform we blank its
    account fields so the resolver falls through to a live scan.
    """
    request = StorageReadRequest(
        storageName=STORAGE_NAME,
        appCode=APP_CODE,
        clientCode=client_code,
        size=_MAPPING_PAGE_SIZE,
    )

    response = await storage_service.read_page_storage(
        request, auth_headers=auth_headers
    )

    if not response.success or not response.content:
        logger.warning("campaign_mapping_no_data client_code=%s", client_code)
        return {}

    records = response.content
    mapping: dict[str, dict[str, Any]] = {}

    for record in records:
        d = record.get("data", record)
        product_id = record.get("_id") or record.get("id") or ""
        summary = d.get("finalSummary", d.get("summary", ""))
        business_url = d.get("businessUrl", "")

        # Extract launch-level metadata from the singular campaign object
        campaign_obj = d.get("campaign") or {}
        launch_platform = campaign_obj.get("platform", "")
        accounts = campaign_obj.get("accounts") or {}
        launch_account_id = (accounts.get("ad") or {}).get("id", "")
        launch_login_id = (accounts.get("parent") or {}).get("id", "")
        launch_platform_norm = normalize_platform(launch_platform)

        brand_info = {
            "brand_name": d.get("productName", ""),
            "business_type": d.get("businessType", ""),
            "primary_location": d.get("location", ""),
            "service_areas": d.get("serviceAreas", []),
        }
        unique_features = d.get("uniqueFeatures", [])

        # Per-campaign mapping (keyed by numeric campaign ID)
        for campaign in d.get("campaigns", []):
            campaign_id = str(campaign.get("campaignId", ""))
            camp_platform = campaign.get("platform", launch_platform)
            camp_platform_norm = normalize_platform(camp_platform)

            # Cross-platform safety: if the campaign's platform differs
            # from the launch platform, the launch account IDs are for
            # the wrong platform.  Leave them empty so the resolver
            # falls through to the live API scan.
            if (
                camp_platform_norm
                and launch_platform_norm
                and camp_platform_norm != launch_platform_norm
            ):
                camp_account_id = ""
                camp_login_id = ""
            else:
                camp_account_id = launch_account_id
                camp_login_id = launch_login_id

            if campaign_id:
                mapping[campaign_id] = {
                    "product_id": product_id,
                    "product_name": d.get("productName", ""),
                    "summary": summary,
                    "business_url": business_url,
                    "platform": camp_platform,
                    "name": campaign.get("name") or campaign.get("campaignName", ""),
                    "brand_info": brand_info,
                    "unique_features": unique_features,
                    "account_id": camp_account_id,
                    "login_customer_id": camp_login_id,
                }

    return mapping
