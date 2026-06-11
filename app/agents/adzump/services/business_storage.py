"""Persist business + campaign data to nocode-saas storage.

Single record per businessUrl in the shared `AISuggestedData` collection
(appCode `marketingai`). Adds a `campaign` sub-object alongside the existing
analysis fields. Latest-launch-wins per URL — no history (yet).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from app.agents.adzump.tools._shared import build_ds_headers
from app.agents.adzump.services.storage_base import storage_service
from app.agents.adzump.services.storage_models import (
    StorageReadRequest,
    StorageFilter,
    StorageRequestWithPayload,
    StorageUpdateWithPayload,
)

logger = logging.getLogger(__name__)


class BusinessStorageService:
    """Handles CRUD for business data (AISuggestedData).
    Aligned with model-driven architecture.
    """

    STORAGE_NAME = "AISuggestedData"
    APP_CODE = "marketingai"
    SCHEMA_VERSION = 1

    async def get_by_url(self, url: str, context: dict) -> dict | None:
        """Return the AISuggestedData record for this URL, or None on miss."""
        if not url:
            return None

        client_code = context.get("client_code", "")
        auth_headers = build_ds_headers(context)

        normalized = self._normalize_url(url)
        request = StorageReadRequest(
            storageName=self.STORAGE_NAME,
            appCode=self.APP_CODE,
            clientCode=client_code,
            filter=StorageFilter(field="businessUrl", value=normalized),
            size=1,
        )

        response = await storage_service.read_page_storage(
            request, auth_headers=auth_headers
        )
        if not response.success:
            logger.info(f"business_storage_read_miss: url={url} err={response.error}")
            return None

        content = response.content
        return content[0] if content else None

    async def save_campaign(self, session_ctx: dict, context: dict) -> str | None:
        """Persist the user's campaign to AISuggestedData.
        Overwrites/merges the `campaign` sub-object.
        """
        url = self.resolve_url(session_ctx)
        if not url:
            logger.warning("save_campaign_skipped: no businessUrl in session")
            return None

        auth_headers = build_ds_headers(context)
        record = self._build_full_record(session_ctx, url)
        existing = await self.get_by_url(url, context)

        if existing:
            existing_id = existing.get("_id") or existing.get("id")
            if existing_id:
                request = StorageUpdateWithPayload(
                    storageName=self.STORAGE_NAME,
                    appCode=self.APP_CODE,
                    dataObjectId=existing_id,
                    dataObject=record,
                    isPartial=True,
                )
                response = await storage_service.update_storage(
                    request, auth_headers=auth_headers
                )
                if response.success:
                    logger.info(
                        f"save_campaign_ok: action=update url={url} id={existing_id}"
                    )
                    return existing_id

                logger.warning(
                    f"save_campaign_update_failed: url={url} err={response.error}"
                )
                return None

        # Create path
        request = StorageRequestWithPayload(
            storageName=self.STORAGE_NAME,
            appCode=self.APP_CODE,
            dataObject=record,
        )
        response = await storage_service.write_storage(
            request, auth_headers=auth_headers
        )
        if not response.success:
            logger.warning(
                f"save_campaign_create_failed: url={url} err={response.error}"
            )
            return None

        new_records = response.content
        new_id = ""
        if new_records:
            new_id = new_records[0].get("_id") or new_records[0].get("id") or ""

        logger.info(f"save_campaign_ok: action=create url={url} id={new_id}")
        return new_id or None

    async def hydrate_from_storage(
        self, url: str, session_ctx: dict, context: dict
    ) -> bool:
        """Hydrate session context from stored AISuggestedData record."""
        record = await self.get_by_url(url, context)
        if not record:
            return False

        # Unwrap data if nested
        d = record.get("data", record) if isinstance(record, dict) else record

        if not session_ctx.get("product_data"):
            session_ctx["product_data"] = self._record_to_business(d)
            session_ctx.setdefault("product_profile", {}).update(
                {
                    "url": d.get("businessUrl") or url,
                    "title": d.get("productName") or d.get("businessName") or url,
                    "summary": d.get("summary", ""),
                }
            )
            logger.info(f"hydrate_from_storage: business loaded url={url}")

        if not session_ctx.get("competitor_analysis"):
            competitors = d.get("competitors") or []
            if competitors:
                session_ctx["competitor_analysis"] = {"competitors": competitors}
                logger.info(
                    f"hydrate_from_storage: {len(competitors)} competitors loaded url={url}"
                )

        return True

    async def fetch_campaign_mappings(
        self, client_code: str, auth_headers: dict
    ) -> dict[str, dict[str, Any]]:
        """Fetch AISuggestedData and build a mapping of campaigns to products.

        Returns:
            {campaign_id: {product_id, summary, business_url, platform,
                           account_id, login_customer_id, ...}}

        Cross-platform safety: the singular ``campaign.accounts`` stores the
        accounts for the **launch** platform.  If a linked campaign in the
        ``campaigns[]`` array belongs to a *different* platform the account
        IDs would be wrong.  We detect this by comparing the per-campaign
        platform against the launch platform and leave account fields empty
        when they differ — forcing the resolver to fall through to a live
        platform scan.
        """
        from app.agents.adzump.agents.optimization.platform_registry import (
            normalize_platform,
        )

        request = StorageReadRequest(
            storageName=self.STORAGE_NAME,
            appCode=self.APP_CODE,
            clientCode=client_code,
            size=5000,
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

            # ── Extract launch-level metadata from the singular campaign object ──
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

            # ── Per-campaign mapping (keyed by numeric campaign ID) ──
            for campaign in d.get("campaigns", []):
                campaign_id = str(campaign.get("campaignId", ""))
                camp_platform = campaign.get("platform", launch_platform)
                camp_platform_norm = normalize_platform(camp_platform)

                # Cross-platform safety: if the campaign's platform differs
                # from the launch platform, the launch account IDs are for
                # the wrong platform.  Leave them empty so the resolver
                # falls through to the live API scan.
                if camp_platform_norm and launch_platform_norm and camp_platform_norm != launch_platform_norm:
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

    # ── Internal Helpers ───────────────────────────────────────────────────

    def _normalize_url(self, url: str) -> str:
        if not url:
            return ""
        p = urlparse(url.strip())
        host = (p.netloc or "").lower().removeprefix("www.")
        path = (p.path or "").rstrip("/")
        if p.scheme:
            return f"{p.scheme}://{host}{path}"
        return f"https://{host}{path}"

    def resolve_url(self, session_ctx: dict) -> str:
        """Find the business URL across the various places it can live."""
        profile = session_ctx.get("product_profile") or {}
        if profile.get("url"):
            return profile["url"]
        product = session_ctx.get("product_data") or {}
        pages = product.get("pages_analyzed") or []
        return pages[0] if pages else ""

    def _build_location_object(self, loc_meta: dict, spec: dict, product: dict) -> dict:
        """Match the legacy ds-v1 location object shape so ds downstream services
        (chatv2 confirm-location, business_service.update lookup, geo-target
        builders) can keep reading the same keys. Map-confirmed location wins,
        then user-typed spec, then scraped-from-website.
        """
        coords = (
            {"lng": loc_meta.get("lng"), "lat": loc_meta.get("lat")}
            if loc_meta.get("lat") is not None and loc_meta.get("lng") is not None
            else None
        )
        return {
            "area_location": "",
            "product_location": (
                loc_meta.get("address")
                or spec.get("location")
                or product.get("location", "")
            ),
            "product_coordinates": coords,
        }

    def _build_map_embeds(self, loc_meta: dict) -> list[dict]:
        """Mirror ds-v1's mapEmbeds entry — empty when we have no coords."""
        if loc_meta.get("lat") is None or loc_meta.get("lng") is None:
            return []
        lat = loc_meta["lat"]
        lng = loc_meta["lng"]
        return [
            {
                "src": (
                    f"https://www.google.com/maps/embed/v1/place?q={lat},{lng}&zoom=15"
                ),
                "title": "",
                "coordinates": {"lng": lng, "lat": lat},
            }
        ]

    def _build_full_record(self, session_ctx: dict, url: str) -> dict[str, Any]:
        product = session_ctx.get("product_data") or {}
        profile = session_ctx.get("product_profile") or {}
        spec = session_ctx.get("campaign_spec") or {}
        loc_meta = session_ctx.get("_location_meta") or {}
        account_names = session_ctx.get("account_names") or {}
        competitive = session_ctx.get("competitor_analysis") or {}

        from app.agents.adzump.platform import is_meta as _platform_is_meta

        is_meta = _platform_is_meta(spec.get("platform"))

        summary = profile.get("summary") or product.get("summary", "")

        return {
            "businessUrl": self._normalize_url(url),
            "summary": summary,
            "finalSummary": summary,
            "businessType": product.get("business_type", ""),
            "location": self._build_location_object(loc_meta, spec, product),
            "mapEmbeds": self._build_map_embeds(loc_meta),
            "screenshot": (
                product.get("primary_screenshot_url")
                or product.get("screenshot_url")
                or product.get("screenshot")
                or ""
            ),
            "externalLinks": product.get("external_links") or [],
            "siteLinks": product.get("site_links") or [],
            "productName": product.get("product_name", ""),
            "businessName": product.get("product_name", ""),
            "uniqueFeatures": product.get("unique_features") or [],
            "productsServices": product.get("products_services") or [],
            "pricing": product.get("pricing", ""),
            "contact": product.get("contact") or {},
            "pagesAnalyzed": product.get("pages_analyzed") or [],
            "competitors": (competitive or {}).get("competitors") or [],
            "lastAnalyzedAt": datetime.now(timezone.utc).isoformat(),
            "lastAnalyzedBy": "adzump-launch",
            "schemaVersion": self.SCHEMA_VERSION,
            "campaign": {
                "savedAt": datetime.now(timezone.utc).isoformat(),
                "sessionId": session_ctx.get("_session_id", ""),
                "status": "launched",
                "platform": spec.get("platform", ""),
                "duration": spec.get("duration", ""),
                "dailyBudget": spec.get("budget", ""),
                "location": {
                    "address": loc_meta.get("address") or spec.get("location", ""),
                    "lat": loc_meta.get("lat"),
                    "lng": loc_meta.get("lng"),
                    "displayName": loc_meta.get("displayName", ""),
                },
                "accounts": {
                    "parent": self._account_pair(
                        spec.get("parent_account"), account_names
                    ),
                    "ad": self._account_pair(spec.get("account"), account_names),
                    "fbPage": self._account_pair(spec.get("fb_page"), account_names)
                    if is_meta
                    else None,
                    "igPage": self._account_pair(spec.get("ig_page"), account_names)
                    if is_meta
                    else None,
                },
                "competitive": {
                    "attempted": session_ctx.get("competitor_analysis") is not None,
                    "declined": spec.get("competitive_analysis_declined") == "true",
                },
            },
        }

    def _account_pair(self, acct_id: Any, account_names: dict) -> dict | None:
        if not acct_id:
            return None
        sid = str(acct_id)
        return {"id": sid, "name": (account_names.get(sid) or "").strip()}

    def _record_to_business(self, d: dict) -> dict:
        screenshot = d.get("screenshot", "")
        raw_loc = d.get("location") or ""
        if isinstance(raw_loc, dict):
            location_str = (
                raw_loc.get("product_location") or raw_loc.get("area_location") or ""
            )
        else:
            location_str = raw_loc
        return {
            "product_name": d.get("productName") or d.get("businessName", ""),
            "business_type": d.get("businessType", ""),
            "summary": d.get("summary", ""),
            "location": location_str,
            "suggested_locations": d.get("suggestedGeoTargets") or [],
            "primary_screenshot_url": screenshot,
            "screenshot_url": screenshot,
            "external_links": d.get("externalLinks") or [],
            "unique_features": d.get("uniqueFeatures") or [],
            "products_services": d.get("productsServices") or [],
            "pricing": d.get("pricing", ""),
            "contact": d.get("contact") or {},
            "pages_analyzed": d.get("pagesAnalyzed") or [],
        }


def parse_location_update(user_message: str) -> dict | None:
    """If the user's message is a `location_update` JSON callback, return
    ``{address, lat, lng}``. Returns None for plain "confirm" or anything
    else.
    """
    if not user_message:
        return None
    msg = user_message.strip()
    # Simple regex check to see if it's a location_update JSON
    if not msg.startswith("{") or '"type"' not in msg or '"location_update"' not in msg:
        return None
    try:
        payload = json.loads(msg)
    except (ValueError, json.JSONDecodeError):
        return None
    if payload.get("type") != "location_update":
        return None
    return {
        "address": (payload.get("address") or "").strip(),
        "lat": payload.get("lat"),
        "lng": payload.get("lng"),
    }


business_storage_service = BusinessStorageService()
