"""Service for managing campaign optimization recommendations in storage."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.agents.adzump.models.optimization import CampaignRecommendation
from app.agents.appbuilder.tools._shared import get_saas_client
from app.agents.adzump.tools._shared import build_ds_headers

from structlog import get_logger

logger = get_logger(__name__)

STORAGE_NAME = "campaignSuggestions"
APP_CODE = "marketingai"

READ_PAGE = "/api/core/function/execute/CoreServices.Storage/ReadPage"
READ = "/api/core/function/execute/CoreServices.Storage/Read"
CREATE = "/api/core/function/execute/CoreServices.Storage/Create"
UPDATE = "/api/core/function/execute/CoreServices.Storage/Update"


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
    data = raw
    for _ in range(2):
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


async def store_recommendation(recommendation: CampaignRecommendation, ctx: dict) -> dict:
    """Persist a recommendation to storage, marking any old one as completed."""
    campaign_id = recommendation.campaign_id
    existing = await fetch_existing_recommendation(campaign_id, ctx)

    base_fields = existing.get("fields", {}) if existing else None

    doc = _build_recommendation_doc(recommendation, base_fields)
    
    # Create new recommendation
    payload = {
        "storageName": STORAGE_NAME,
        "appCode": APP_CODE,
        "dataObject": doc,
    }
    result = await get_saas_client().post(
        CREATE, headers=_storage_headers(ctx), json=payload
    )
    
    if result.success:
        logger.info("recommendation_created", campaign_id=campaign_id)
    else:
        logger.warning("recommendation_create_failed: campaign=%s err=%s", 
                       campaign_id, result.error)

    # Mark existing as completed
    if existing:
        existing_id = existing.get("_id") or existing.get("id")
        if existing_id:
            await mark_recommendation_completed(existing_id, ctx)
            logger.info("recommendation_previous_completed", 
                        campaign_id=campaign_id, record_id=existing_id)

    return {"fields": doc["fields"]}


async def fetch_existing_recommendation(campaign_id: str, ctx: dict) -> dict | None:
    """Find an active (non-completed) recommendation for a campaign."""
    payload = {
        "storageName": STORAGE_NAME,
        "appCode": APP_CODE,
        "clientCode": ctx.get("client_code", ""),
        "filter": {
            "operator": "AND",
            "conditions": [
                {"field": "campaign_id", "value": campaign_id},
                {"field": "completed", "operator": "IS_FALSE"},
            ],
        },
        "size": 1,
    }
    result = await get_saas_client().post(
        READ_PAGE, headers=_storage_headers(ctx), json=payload
    )
    if not result.success:
        return None
        
    records = _extract_records(result.data)
    return records[0] if records else None


async def mark_recommendation_completed(record_id: str, ctx: dict):
    """Mark a specific recommendation document as completed."""
    payload = {
        "storageName": STORAGE_NAME,
        "appCode": APP_CODE,
        "dataObjectId": record_id,
        "dataObject": {"completed": True},
    }
    return await get_saas_client().post(
        UPDATE, headers=_storage_headers(ctx), json=payload
    )


async def get_recommendation_by_id(record_id: str, ctx: dict) -> dict | None:
    """Fetch a specific recommendation by its document ID."""
    payload = {
        "storageName": STORAGE_NAME,
        "appCode": APP_CODE,
        "clientCode": ctx.get("client_code", ""),
        "dataObjectId": record_id,
    }
    result = await get_saas_client().post(
        READ, headers=_storage_headers(ctx), json=payload
    )
    if not result.success:
        return None
        
    records = _extract_records(result.data)
    return records[0] if records else None


def _build_recommendation_doc(rec: CampaignRecommendation, base_fields: dict | None) -> dict:
    """Convert model to storage document shape."""
    fields = _merge_fields(rec, base_fields)
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "platform": rec.platform,
        "parent_account_id": rec.parent_account_id,
        "account_id": rec.account_id,
        "product_id": rec.product_id,
        "campaign_id": rec.campaign_id,
        "campaign_name": rec.campaign_name,
        "campaign_type": rec.campaign_type,
        "completed": False,
        "fields": fields,
    }


def _merge_fields(rec: CampaignRecommendation, base_fields: dict | None) -> dict:
    """Merge new recommendations into existing ones, preserving non-conflicting items."""
    fields = dict[Any, Any](base_fields) if base_fields else {}
    rec_fields = rec.fields.model_dump(exclude_none=True)
    
    for key, new_items in rec_fields.items():
        for item in new_items:
            item["applied"] = False
            
        existing = fields.get(key, [])
        if key in ("keywords", "negativeKeywords"):
            origins = {item.get("origin") for item in new_items if item.get("origin")}
            kept = [item for item in existing if item.get("origin") not in origins]
            fields[key] = kept + new_items
        else:
            fields[key] = new_items
    return fields


async def apply_mutation_results(
    recommendation: CampaignRecommendation,
    is_partial: bool,
    ctx: dict,
) -> CampaignRecommendation:
    """Mark items as applied locally and sync with storage."""
    fields = recommendation.fields
    updated_data = {
        name: [
            item.model_copy(update={"applied": True})
            for item in getattr(fields, name)
        ]
        for name in fields.model_fields.keys()
        if getattr(fields, name)
    }
    updated_fields_obj = fields.model_copy(update=updated_data)

    new_completed = not is_partial
    updated_recommendation = recommendation.model_copy(
        update={"fields": updated_fields_obj, "completed": new_completed}
    )

    if updated_recommendation._id:
        await sync_mutation_result(updated_recommendation, is_partial, ctx)

    return updated_recommendation


async def sync_mutation_result(
    recommendation: CampaignRecommendation,
    is_partial: bool,
    ctx: dict,
):
    """Update storage by merging applied status from the current mutation into the full record."""
    rec_id = recommendation._id
    if not rec_id:
        return

    if is_partial:
        existing = await get_recommendation_by_id(rec_id, ctx)
        if not existing:
            logger.error("storage_sync_failed_record_missing", record_id=rec_id)
            return

        applied_fields_dump = recommendation.fields.model_dump(exclude_none=True)
        fields_to_store = _merge_applied_status(existing.get("fields", {}), applied_fields_dump)
    else:
        fields_to_store = recommendation.fields.model_dump(exclude_none=True)

    payload = {
        "storageName": STORAGE_NAME,
        "appCode": APP_CODE,
        "dataObjectId": rec_id,
        "dataObject": {
            "fields": fields_to_store,
            "completed": recommendation.completed,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        },
        "isPartial": True,
    }
    await get_saas_client().post(UPDATE, headers=_storage_headers(ctx), json=payload)


def _merge_applied_status(existing_fields: dict, incoming_applied_fields: dict) -> dict:
    """Match items and mark them as applied in the stored fields."""
    ID_KEYS = ["resource_name", "text", "geo_target_constant", "age_range", "gender_type", "link_text"]

    def get_uid(item):
        return next((item[k] for k in ID_KEYS if item.get(k)), None)

    for field_name, applied_items in incoming_applied_fields.items():
        if field_name not in existing_fields:
            existing_fields[field_name] = applied_items
            continue

        stored_items = existing_fields[field_name]
        applied_ids = {get_uid(item) for item in applied_items if get_uid(item)}

        for stored_item in stored_items:
            if get_uid(stored_item) in applied_ids:
                stored_item["applied"] = True

    return existing_fields
