"""Shared helpers to resolve campaign platform and owning account.

Centralizes the logic previously duplicated between the chat tool and the
OptimizationAgent. Returns platform, whether a product mapping exists, the
mapping itself (if any) and account metadata when resolvable.

Resolution order (strict campaign-driven, zero session leaks):
  1. AISuggestedData mapping — exact match by campaign_id
  2. Stored recommendation fallback — previous successful run
  3. AISuggestedData fuzzy name match — campaign_name / product_name
  4. Live platform scan — API calls to Google / Meta (ground truth)

Session context is NEVER consulted for platform, account_id,
login_customer_id, product_id, or product_name.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.adzump.agents.optimization.platform_registry import (
    normalize_platform,
)
from app.agents.adzump.agents.optimization.platform_handlers import (
    resolve_campaign_account_for_platform,
)

logger = logging.getLogger(__name__)


async def resolve_platform_and_account(
    campaign_id: str,
    client_code: str,
    auth_headers: dict,
    session_context: dict,
    live_scan: bool = True,
) -> dict[str, Any]:
    """Resolve platform, mapping presence and owning account for a campaign.

    All resolved fields come strictly from the campaign's own data
    (AISuggestedData mapping, stored recommendations, or live API scan).
    The ``session_context`` parameter is kept for backward compatibility
    but is NOT consulted for platform, account, or product fields.

    Returns a dict with keys:
      - campaign_id: str (may be updated if resolved from a name lookup)
      - platform: normalized platform string or None
      - mapping: mapping dict or None
      - mapping_exists: bool
      - account_id: str or empty
      - login_customer_id: str or empty
      - product_id: str or empty
      - product_name: str or empty
    """
    platform = None
    mapping = None
    mapping_exists = False
    account_id = ""
    login_customer_id = ""
    product_id = ""
    product_name = ""

    # Step 1: AISuggestedData mapping (exact match by campaign_id)
    mappings = None
    try:
        from app.agents.adzump.services.business_storage import (
            business_storage_service,
        )

        mappings = await business_storage_service.fetch_campaign_mappings(
            client_code, auth_headers
        )
        mapping = mappings.get(campaign_id)
        if mapping:
            mapping_exists = True
            plat_str = mapping.get("platform")
            if plat_str:
                platform = normalize_platform(plat_str)
            product_id = mapping.get("product_id", "")
            product_name = mapping.get("product_name", "")
            # NOTE: We intentionally do NOT extract account_id /
            # login_customer_id from the mapping here.  The mapping's
            # accounts come from the AISuggestedData launch record
            # (campaign.accounts) and represent the account used at
            # launch time — NOT necessarily the account that owns this
            # specific campaign.  Even within the same platform (both
            # GOOGLE) the user may have multiple ad accounts.  The
            # live platform scan (Step 4) is the only ground truth.
    except Exception:
        logger.warning(
            "resolver: fetch_campaign_mappings failed campaign=%s",
            campaign_id,
            exc_info=True,
        )

    # Step 2: Stored recommendation fallback
    # Use stored recs for platform + product details.  Account IDs from
    # stored recs are saved as a last-resort fallback (used only if the
    # live scan in Step 4 fails).
    stored_account_id = ""
    stored_login_customer_id = ""
    if not platform:
        try:
            from app.agents.adzump.services.recommendation_storage import (
                recommendation_storage_service,
            )

            existing = await recommendation_storage_service.get_latest(
                campaign_id, client_code, auth_headers=auth_headers
            )
            if existing:
                resolved_cid = (
                    existing.get("campaignId")
                    or existing.get("campaign_id")
                    or campaign_id
                )
                # Update campaign_id if we resolved from a name/product lookup
                campaign_id = resolved_cid

                if not platform:
                    plat_str = existing.get("platform")
                    if plat_str:
                        platform = normalize_platform(plat_str)
                if not product_id:
                    product_id = (
                        existing.get("productId") or existing.get("product_id") or ""
                    )
                if not product_name:
                    product_name = (
                        existing.get("productName")
                        or existing.get("product_name")
                        or ""
                    )
                # Save account IDs as last-resort fallback (only used if
                # live scan fails in Step 4)
                stored_account_id = (
                    existing.get("account_id") or existing.get("accountId") or ""
                )
                stored_login_customer_id = (
                    existing.get("parent_account_id")
                    or existing.get("parentAccountId")
                    or ""
                )

                # If we resolved a different campaign_id, try its mapping too
                if mappings and campaign_id in mappings and not mapping_exists:
                    mapping = mappings[campaign_id]
                    mapping_exists = True
                    if not platform:
                        plat_str = mapping.get("platform")
                        if plat_str:
                            platform = normalize_platform(plat_str)
                    if not product_id:
                        product_id = mapping.get("product_id", "")
                    if not product_name:
                        product_name = mapping.get("product_name", "")
        except Exception:
            logger.debug("resolver: recommendation fallback failed", exc_info=True)

    # Step 3: Fuzzy name match in mappings
    if not mapping_exists and mappings:
        for k, val in mappings.items():
            camp_name = val.get("name") or ""
            prod_name = val.get("product_name") or ""
            if (
                camp_name.strip().lower() == campaign_id.strip().lower()
                or prod_name.strip().lower() == campaign_id.strip().lower()
            ):
                campaign_id = k
                mapping = val
                mapping_exists = True
                if not platform:
                    plat_str = mapping.get("platform")
                    if plat_str:
                        platform = normalize_platform(plat_str)
                if not product_id:
                    product_id = val.get("product_id", "")
                if not product_name:
                    product_name = val.get("product_name", "")
                # NOTE: account_id / login_customer_id intentionally NOT
                # extracted here — same reason as Step 1 (launch account
                # may differ from campaign-owning account).
                break

    # Step 4: Live platform scan (ALWAYS run — ground truth)
    # The live scan queries the actual ad platform API to find which account
    # owns this campaign.  This is the ONLY reliable source for account_id
    # and login_customer_id because:
    #   - The AISuggestedData mapping stores the LAUNCH account, not the
    #     owning account (even same-platform, different accounts possible)
    #   - Stored recs might be stale if the campaign was moved between accounts
    if live_scan:
        try:
            account_details = await resolve_campaign_account_for_platform(
                platform, campaign_id, client_code, auth_headers
            )
            if account_details:
                resolved_platform = normalize_platform(account_details.get("platform"))
                if not platform:
                    platform = resolved_platform
                elif resolved_platform and resolved_platform != platform:
                    # Live scan found the campaign on a different platform
                    # than what the mapping/stored rec said — trust the live
                    # scan since it's ground truth from the ad platform API.
                    logger.warning(
                        "resolver: platform_corrected campaign=%s "
                        "mapping_said=%s live_scan_found=%s",
                        campaign_id,
                        platform,
                        resolved_platform,
                    )
                    platform = resolved_platform
                account_id = account_details.get("account_id", "")
                login_customer_id = account_details.get("login_customer_id", "")
        except Exception:
            logger.warning(
                "resolver: live scan failed campaign=%s, "
                "falling back to stored account IDs",
                campaign_id,
                exc_info=True,
            )

    # Last resort: use stored rec account IDs if live scan failed
    if not account_id and stored_account_id:
        account_id = stored_account_id
        logger.info(
            "resolver: using stored_rec account_id=%s campaign=%s",
            account_id,
            campaign_id,
        )
    if not login_customer_id and stored_login_customer_id:
        login_customer_id = stored_login_customer_id
        logger.info(
            "resolver: using stored_rec login_customer_id=%s campaign=%s",
            login_customer_id,
            campaign_id,
        )

    return {
        "campaign_id": campaign_id,
        "platform": platform,
        "mapping": mapping,
        "mapping_exists": mapping_exists,
        "account_id": account_id,
        "login_customer_id": login_customer_id,
        "product_id": product_id,
        "product_name": product_name,
    }
