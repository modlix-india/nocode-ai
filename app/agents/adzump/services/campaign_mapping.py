"""Service for fetching campaign-to-product mappings from storage."""

from __future__ import annotations

from structlog import get_logger
from typing import Any

from app.agents.appbuilder.tools._shared import get_saas_client
from app.agents.adzump.tools._shared import build_ds_headers

logger = get_logger(__name__)

STORAGE_NAME = "AISuggestedData"
APP_CODE = "marketingai"

READ_PAGE = "/api/core/function/execute/CoreServices.Storage/ReadPage"


def _storage_headers(ctx: dict) -> dict[str, str]:
    """Auth headers for storage calls. AppCode pinned to ``marketingai``."""
    h = build_ds_headers(ctx)
    h["AppCode"] = APP_CODE
    h["Content-Type"] = "application/json"
    return h


def _extract_records(raw: Any) -> list[dict]:
    """Extract records from the standard StorageResponse envelope."""
    if raw is None:
        return []

    # If it's a list, unwrap the first 'output' envelope
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and "result" in item:
                raw = item["result"]
                break

    # Drill through nested 'result' keys (up to 3 levels)
    data = raw
    for _ in range(3):
        if isinstance(data, dict) and "result" in data:
            data = data["result"]
        else:
            break

    if data is None:
        return []

    if isinstance(data, dict) and "content" in data:
        content = data["content"]
        return content if isinstance(content, list) else [content]

    return data if isinstance(data, list) else [data]


async def get_campaign_product_mapping(ctx: dict) -> dict[str, str]:
    """
    Returns: {campaign_id: product_id}
    """
    payload = {
        "storageName": STORAGE_NAME,
        "appCode": APP_CODE,
        "clientCode": ctx.get("client_code", ""),
        "filter": None,
        "size": 200,
    }

    result = await get_saas_client().post(
        READ_PAGE, headers=_storage_headers(ctx), json=payload
    )

    if not result.success:
        logger.warning(
            "campaign_mapping_fetch_failed: client=%s err=%s",
            ctx.get("client_code"),
            result.error,
        )
        return {}

    records = _extract_records(result.data)
    return _build_mapping(records)


async def get_campaign_mapping_with_summary(ctx: dict) -> tuple[dict, dict | None]:
    """
    Returns: (
        {campaign_id: {"product_id": str, "summary": str, "business_url": str}},
        fallback_entry | None   <- used when no campaign linkage exists in storage
    )
    """
    client_code = ctx.get("client_code") or ctx.get("session_context", {}).get(
        "client_code", ""
    )

    logger.info(
        "campaign_mapping_fetch_attempt",
        client=client_code,
    )

    payload = {
        "storageName": STORAGE_NAME,
        "appCode": APP_CODE,
        "clientCode": client_code,
        "filter": None,
        "size": 200,
    }

    result = await get_saas_client().post(
        READ_PAGE, headers=_storage_headers(ctx), json=payload
    )

    if not result.success:
        logger.warning(
            "campaign_mapping_fetch_failed",
            client=client_code,
            error=result.error,
        )
        return {}, None

    records = _extract_records(result.data)

    logger.info("campaign_mapping_records_extracted", count=len(records))

    if records:
        logger.info(
            "campaign_mapping_first_record_debug",
            keys=list(records[0].keys()),
        )

    return _build_mapping_with_summary(records)


def _get_record_fields(record: dict) -> dict:
    """Unwrap storage record fields (handles both flat and 'data'-wrapped shapes)."""
    if isinstance(record.get("data"), dict):
        return record["data"]
    return record


def _build_mapping(records: list) -> dict[str, str]:
    """Build campaign_id → product_id mapping."""
    mapping = {}
    for record in records:
        product_id = record.get("_id") or record.get("id")
        fields = _get_record_fields(record)

        for campaign in fields.get("campaigns", []):
            campaign_id = str(
                campaign.get("campaignId") or campaign.get("campaign_id") or ""
            ).split("/")[-1]
            if campaign_id:
                mapping[campaign_id] = product_id

        campaign = fields.get("campaign")
        if isinstance(campaign, dict):
            campaign_id = str(
                campaign.get("campaignId") or campaign.get("campaign_id") or ""
            ).split("/")[-1]
            if campaign_id:
                mapping[campaign_id] = product_id

    return mapping


def _build_mapping_with_summary(records: list) -> tuple[dict, dict | None]:
    """
    Build campaign_id → {product_id, summary, business_url} mapping.
    Also returns a fallback_entry if no campaign linkage exists in storage,
    so the summary can still be used for all campaigns.
    """
    mapping = {}
    fallback_entry = None

    for record in records:
        product_id = record.get("_id") or record.get("id")
        fields = _get_record_fields(record)

        summary = fields.get("finalSummary") or fields.get("summary") or ""
        business_url = fields.get("businessUrl") or fields.get("business_url") or ""

        entry = {
            "product_id": product_id,
            "summary": summary,
            "business_url": business_url,
        }

        # Check plural 'campaigns' list
        for campaign in fields.get("campaigns", []):
            campaign_id = str(
                campaign.get("campaignId") or campaign.get("campaign_id") or ""
            ).split("/")[-1]
            if campaign_id:
                mapping[campaign_id] = entry

        # Check singular 'campaign' object
        campaign = fields.get("campaign")
        if isinstance(campaign, dict):
            campaign_id = str(
                campaign.get("campaignId") or campaign.get("campaign_id") or ""
            ).split("/")[-1]
            if campaign_id:
                mapping[campaign_id] = entry

        # Save as fallback if we have a summary and don't have one yet (even if it has campaigns)
        if summary and fallback_entry is None:
            fallback_entry = entry
            logger.info(
                "campaign_mapping_fallback_set",
                product_id=product_id,
                summary_length=len(summary),
            )

    logger.info(
        "campaign_mapping_built",
        final_count=len(mapping),
        has_fallback=bool(fallback_entry),
    )
    return mapping, fallback_entry
