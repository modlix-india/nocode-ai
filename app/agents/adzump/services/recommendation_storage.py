"""Storage service for persisting optimization recommendations."""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import TypeAdapter

from app.agents.adzump.recommendations.models import (
    CampaignRecommendation,
    RecommendationStatus,
    WorkflowItem,
)
from app.agents.adzump.services.storage_base import storage_service
from app.agents.adzump.services.storage_models import (
    StorageReadRequest,
    StorageRequestWithPayload,
    StorageUpdateWithPayload,
    FilterCondition,
    ComplexCondition,
    FilterOperator,
    LogicalOperator,
    SortOrder,
    SortDirection,
)

logger = logging.getLogger(__name__)

_SORT_UPDATED_AT_DESC = [SortOrder(property="updatedAt", direction=SortDirection.DESC)]


def snake_to_camel(s: str) -> str:
    if not isinstance(s, str):
        return s
    components = s.split("_")
    return components[0] + "".join(x.title() for x in components[1:])


def camelize_dict(obj: Any) -> Any:
    if isinstance(obj, dict):
        new_dict = {}
        for k, v in obj.items():
            new_key = snake_to_camel(k) if isinstance(k, str) else k
            new_dict[new_key] = camelize_dict(v)
        return new_dict
    elif isinstance(obj, list):
        return [camelize_dict(item) for item in obj]
    else:
        return obj


class RecommendationStorageService:
    """CRUD operations for campaignSuggestions storage in nocode-saas.
    Stores, reads, queries, and updates CampaignRecommendation records.
    """

    STORAGE_NAME = "campaignSuggestions"
    APP_CODE = "marketingai"

    async def store(
        self,
        recommendation: "CampaignRecommendation",
        client_code: str,
        auth_headers: dict = None,
    ) -> dict:
        campaign_id = recommendation.campaign_id
        existing = await self._fetch_existing(
            campaign_id, client_code, auth_headers=auth_headers
        )

        base_fields = existing.get("fields", {}) if existing else None

        product_name = recommendation.product_name
        if not product_name and existing:
            product_name = existing.get("productName") or existing.get(
                "product_name", ""
            )

        doc = self._build_recommendation(recommendation, base_fields, product_name)
        response = await self._create(
            doc, client_code=client_code, auth_headers=auth_headers
        )
        if not response.success:
            raise Exception(
                f"Failed to create campaign recommendation in database: {response.error}"
            )

        # Retrieve the newly created record ID to deactivate other duplicates
        new_record_id = None
        records = response.content
        if records:
            new_record_id = records[0].get("_id") or records[0].get("id")

        logger.info(
            "recommendation_created campaign_id=%s product_name=%s record_id=%s",
            campaign_id,
            product_name,
            new_record_id,
        )

        # Clean up older active duplicate records for safety
        await self._deactivate_older_recommendations(
            campaign_id, client_code, new_record_id, auth_headers=auth_headers
        )

        return {"fields": doc["fields"]}

    async def _fetch_existing(
        self, campaign_id: str, client_code: str, auth_headers: dict = None
    ) -> dict | None:
        request = StorageReadRequest(
            storageName=self.STORAGE_NAME,
            appCode=self.APP_CODE,
            clientCode=client_code,
            filter=ComplexCondition(
                operator=LogicalOperator.AND,
                conditions=[
                    FilterCondition(field="campaignId", value=campaign_id),
                    FilterCondition(
                        field="completed", operator=FilterOperator.IS_FALSE
                    ),
                ],
            ),
            sort=_SORT_UPDATED_AT_DESC,
            size=10,
        )
        response = await storage_service.read_page_storage(
            request, auth_headers=auth_headers
        )
        content = response.content
        if not content:
            # TODO: Remove this fallback once the scheduler has run in production and all
            # existing records have been migrated to camelCase keys. In production, the
            # old records will be deleted and the scheduler will re-populate with camelCase.
            request_snake = StorageReadRequest(
                storageName=self.STORAGE_NAME,
                appCode=self.APP_CODE,
                clientCode=client_code,
                filter=ComplexCondition(
                    operator=LogicalOperator.AND,
                    conditions=[
                        FilterCondition(field="campaign_id", value=campaign_id),
                        FilterCondition(
                            field="completed", operator=FilterOperator.IS_FALSE
                        ),
                    ],
                ),
                sort=_SORT_UPDATED_AT_DESC,
                size=10,
            )
            response_snake = await storage_service.read_page_storage(
                request_snake, auth_headers=auth_headers
            )
            content = response_snake.content

        records = [r for r in content if isinstance(r, dict)]
        return records[0] if records else None

    def _build_recommendation(
        self,
        rec: "CampaignRecommendation",
        base_fields: dict | None,
        product_name: str = "",
    ) -> dict:
        fields = self._merge_fields(rec, base_fields)
        return {
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "platform": rec.platform,
            "parentAccountId": rec.parent_account_id,
            "accountId": rec.account_id,
            "productId": rec.product_id,
            "productName": product_name,
            "campaignId": rec.campaign_id,
            "campaignName": rec.campaign_name,
            "campaignType": rec.campaign_type,
            "campaignStatus": rec.campaign_status,
            "adsetCount": rec.adset_count,
            "adCount": rec.ad_count,
            "completed": False,
            "active": True,
            "source": rec.source,
            "generatedAt": rec.generated_at or datetime.now(timezone.utc).isoformat(),
            "fields": fields,
        }

    def _merge_fields(
        self, rec: "CampaignRecommendation", base_fields: dict | None
    ) -> dict:
        from app.agents.adzump.agents.optimization.platform_registry import get_provider

        provider = get_provider(rec.platform)
        if not provider or not provider.capabilities.has_recommendations:
            # Fallback to simple dict merge for platforms without structured merge support
            fields = camelize_dict(base_fields) if base_fields else {}
            rec_fields = rec.fields.model_dump(exclude_none=True, by_alias=True)
            fields.update(rec_fields)
            return fields

        # Parse base_fields back to platform fields model for structured merging
        existing_fields = None
        if base_fields:
            try:
                # Wrap fields inside a mock recommendation for polymorphic parsing
                mock_rec = {
                    "platform": rec.platform,
                    "campaignId": rec.campaign_id,
                    "campaignName": rec.campaign_name,
                    "fields": base_fields,
                }
                existing_rec = TypeAdapter(CampaignRecommendation).validate_python(
                    mock_rec
                )
                existing_fields = existing_rec.fields
            except Exception:
                logger.warning(
                    "Fields validation failed on merge, skipping existing history",
                    exc_info=True,
                )

        merged_fields = provider.merge_fields(
            existing_fields, rec.fields, rec.id or "", campaign_id=rec.campaign_id
        )
        return merged_fields.model_dump(by_alias=True, exclude_none=True)

    async def _create(
        self, doc: dict, client_code: str = None, auth_headers: dict = None
    ):
        request = StorageRequestWithPayload(
            storageName=self.STORAGE_NAME,
            appCode=self.APP_CODE,
            clientCode=client_code,
            dataObject=doc,
        )
        return await storage_service.write_storage(request, auth_headers=auth_headers)

    async def _mark_completed(
        self, record_id: str, client_code: str = None, auth_headers: dict = None
    ):
        request = StorageUpdateWithPayload(
            storageName=self.STORAGE_NAME,
            appCode=self.APP_CODE,
            clientCode=client_code,
            dataObjectId=record_id,
            dataObject={"completed": True, "active": False},
            isPartial=True,
        )
        return await storage_service.update_storage(request, auth_headers=auth_headers)

    async def _deactivate_older_recommendations(
        self,
        campaign_id: str,
        client_code: str,
        active_record_id: Optional[str],
        auth_headers: dict = None,
    ):
        """Clean up older active records to prevent duplicates in case of races/network timeouts."""
        request = StorageReadRequest(
            storageName=self.STORAGE_NAME,
            appCode=self.APP_CODE,
            clientCode=client_code,
            filter=ComplexCondition(
                operator=LogicalOperator.AND,
                conditions=[
                    FilterCondition(field="campaignId", value=campaign_id),
                    FilterCondition(
                        field="completed", operator=FilterOperator.IS_FALSE
                    ),
                ],
            ),
            size=1000,  # TODO: add cursor-based pagination when clients have many campaigns
        )
        response = await storage_service.read_page_storage(
            request, auth_headers=auth_headers
        )
        content = response.content or []

        for record in content:
            if not isinstance(record, dict):
                continue
            rec_id = record.get("_id") or record.get("id")
            if rec_id and rec_id != active_record_id:
                # Mark older active records as completed=True and active=False
                update_req = StorageUpdateWithPayload(
                    storageName=self.STORAGE_NAME,
                    appCode=self.APP_CODE,
                    clientCode=client_code,
                    dataObjectId=rec_id,
                    dataObject={"completed": True, "active": False},
                    isPartial=True,
                )
                await storage_service.update_storage(
                    update_req, auth_headers=auth_headers
                )
                logger.info(
                    "Deactivated older active duplicate record campaign_id=%s id=%s",
                    campaign_id,
                    rec_id,
                )

    # Mutation Sync Logic

    async def apply_mutation_results(
        self,
        recommendation: "CampaignRecommendation",
        client_code: str,
        is_partial: bool,
        accepted_fingerprints: set[str] | None = None,
        auth_headers: dict = None,
    ) -> "CampaignRecommendation":
        """Mark accepted items as applied, handle completion status, and sync with storage.

        Called by the mutation endpoint AFTER Google Ads API mutations complete successfully.
        NOT called by the scheduler — the scheduler only writes fresh recommendations via store().

        Caller contract (mutation endpoint, not yet implemented):
          1. Fetch the record from storage: existing = await storage.get_latest(...)
          2. Set recommendation.record_version = existing["updatedAt"]  ← OCC token
          3. Build mutation payloads for each accepted item (by fingerprint)
          4. Call Google Ads API mutations
          5. Call this method with the fingerprints of successfully mutated items only.
             Items that failed mutation should be excluded from accepted_fingerprints so
             their status remains PENDING (or can be set to FAILED separately).

        accepted_fingerprints: fingerprints of items the Google Ads API accepted.
            Pass None to mark all items applied (full-apply, no partial selection).
        is_partial: True if the user selected a subset of recommendations; False means
            the full record is complete and should be retired (active=False).
        """
        fields = recommendation.fields
        updated_data = {}

        def _should_apply(item: object) -> bool:
            if accepted_fingerprints is None:
                return True
            fp = getattr(item, "fingerprint", "")
            return bool(fp) and fp in accepted_fingerprints

        for name in fields.model_fields.keys():
            value = getattr(fields, name)
            if value is None:
                continue
            if isinstance(value, list):
                updated_data[name] = [
                    item.model_copy(
                        update={"applied": True, "status": RecommendationStatus.APPLIED}
                    )
                    if isinstance(item, WorkflowItem) and _should_apply(item)
                    else item
                    for item in value
                ]
            elif isinstance(value, WorkflowItem) and _should_apply(value):
                updated_data[name] = value.model_copy(
                    update={"applied": True, "status": RecommendationStatus.APPLIED}
                )

        updated_fields_obj = fields.model_copy(update=updated_data)

        new_completed = not is_partial
        updated_recommendation = recommendation.model_copy(
            update={
                "fields": updated_fields_obj,
                "completed": new_completed,
                "active": not new_completed,
            }
        )

        # Sync with Storage (Perform a Merge-Update)
        rec_id = updated_recommendation.id
        if rec_id:
            await self.sync_mutation_result(
                updated_recommendation,
                client_code,
                is_partial,
                auth_headers=auth_headers,
            )

        return updated_recommendation

    async def sync_mutation_result(
        self,
        recommendation: "CampaignRecommendation",
        client_code: str,
        is_partial: bool,
        auth_headers: dict = None,
    ):
        """Persist mutation results: merge applied status into the stored record.

        Internal — called only by apply_mutation_results(). Do not call directly.
        OCC check runs for partial applies: rejects the write if another process
        modified the record between when the caller fetched it and now.
        Requires recommendation.record_version to be set to the DB's updatedAt
        value at fetch time (see apply_mutation_results caller contract).
        """
        if not recommendation.id:
            return

        if is_partial:
            existing = await self._fetch_by_id(
                recommendation.id, client_code, auth_headers=auth_headers
            )
            if not existing:
                logger.error(
                    f"storage_sync_failed_record_missing id={recommendation.id}"
                )
                return

            # OCC check: reject if the record was modified between the caller's fetch and now.
            # record_version must be the DB's updatedAt captured at fetch time (not generated_at).
            # Empty record_version = OCC skipped (safe default for paths that don't set it).
            db_updated_at = existing.get("updatedAt") or existing.get("updated_at")
            client_updated_at = recommendation.record_version  # empty = skip OCC
            if db_updated_at and client_updated_at:
                try:
                    db_dt = datetime.fromisoformat(db_updated_at.replace("Z", "+00:00"))
                    client_dt = datetime.fromisoformat(
                        client_updated_at.replace("Z", "+00:00")
                    )
                    if db_dt > client_dt:
                        logger.warning(
                            "OCC check: stale update rejected id=%s db_updated=%s record_version=%s",
                            recommendation.id,
                            db_updated_at,
                            client_updated_at,
                        )
                        raise Exception(
                            "Optimistic concurrency violation: The recommendation was modified by another process."
                        )
                except ValueError:
                    logger.warning(
                        "OCC date parsing failed, skipping check id=%s db=%s record_version=%s",
                        recommendation.id,
                        db_updated_at,
                        client_updated_at,
                    )
                except Exception as ex:
                    if "concurrency" in str(ex):
                        raise
                    logger.error("OCC unexpected error: %s", ex)

            # Merge 'applied' status into existing fields
            applied_fields_dump = recommendation.fields.model_dump(
                exclude_none=True, by_alias=True
            )
            fields_to_store = self._merge_applied_status(
                existing.get("fields", {}), applied_fields_dump
            )
        else:
            fields_to_store = recommendation.fields.model_dump(
                exclude_none=True, by_alias=True
            )

        request = StorageUpdateWithPayload(
            storageName=self.STORAGE_NAME,
            appCode=self.APP_CODE,
            clientCode=client_code,
            dataObjectId=recommendation.id,
            dataObject={
                "fields": fields_to_store,
                "completed": recommendation.completed,
                "active": recommendation.active,
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            },
            isPartial=True,
        )
        await storage_service.update_storage(request, auth_headers=auth_headers)

    def _merge_applied_status(
        self, existing_fields: dict, incoming_applied_fields: dict
    ) -> dict:
        # existing_fields may be stored in snake_case by older records; normalize it
        existing_fields = camelize_dict(existing_fields)
        # incoming_applied_fields is already by_alias=True (camelCase) from model_dump

        ID_KEYS = [
            "fingerprint",
            "resourceName",
            "text",
            "geoTargetConstant",
            "ageRange",
            "genderType",
            "gender",
            "linkText",
        ]

        def get_uid(item):
            return next((item[k] for k in ID_KEYS if item.get(k)), None)

        for field_name, applied_items in incoming_applied_fields.items():
            if field_name not in existing_fields:
                existing_fields[field_name] = applied_items
                continue

            stored_items = existing_fields[field_name]
            if isinstance(stored_items, list):
                applied_ids = {get_uid(item) for item in applied_items if get_uid(item)}
                for stored_item in stored_items:
                    if get_uid(stored_item) in applied_ids:
                        stored_item["applied"] = True
                        stored_item["status"] = "applied"
            else:
                stored_items["applied"] = True
                stored_items["status"] = "applied"

        return existing_fields

    async def _fetch_by_id(
        self, record_id: str, client_code: str, auth_headers: dict = None
    ) -> dict | None:
        request = StorageReadRequest(
            storageName=self.STORAGE_NAME,
            appCode=self.APP_CODE,
            clientCode=client_code,
            filter=FilterCondition(field="_id", value=record_id),
            size=1,
        )
        response = await storage_service.read_page_storage(
            request, auth_headers=auth_headers
        )
        content = response.content
        if not content:
            return None
        record = content[0]
        return record if isinstance(record, dict) else None

    async def get_latest(
        self, campaign_id: str, client_code: str, auth_headers: dict = None
    ) -> dict | None:
        """Fetch the most recent active CampaignRecommendation dict for a campaign.

        Returns a raw dict (not a model) so the caller can deserialise with
        whichever CampaignRecommendation model it uses. Returns None when
        no active (completed=False) record exists.
        """
        if not campaign_id:
            return None

        cleaned = campaign_id.strip()
        is_numeric = cleaned.isdigit()

        logger.info(
            "recommendation_storage: get_latest campaign=%s lookup_type=%s",
            cleaned,
            "by_id" if is_numeric else "by_name",
        )

        if is_numeric:
            # 1) Try exact lookup by campaign_id
            record = await self._fetch_existing(
                cleaned, client_code, auth_headers=auth_headers
            )
            if record:
                logger.info(
                    "recommendation_storage: get_latest HIT by campaign_id=%s",
                    cleaned,
                )
                return record
        else:
            # 2) Try lookup by campaign_name
            record = await self._fetch_by_campaign_name(
                cleaned, client_code, auth_headers=auth_headers
            )
            if record:
                logger.info(
                    "recommendation_storage: get_latest HIT by campaign_name='%s' "
                    "resolved_id=%s",
                    cleaned,
                    record.get("campaignId") or record.get("campaign_id", "?"),
                )
                return record

            # 3) Try lookup by product_name
            record = await self._fetch_by_product_name(
                cleaned, client_code, auth_headers=auth_headers
            )
            if record:
                logger.info(
                    "recommendation_storage: get_latest HIT by product_name='%s' "
                    "resolved_id=%s",
                    cleaned,
                    record.get("campaignId") or record.get("campaign_id", "?"),
                )
                return record

        logger.info(
            "recommendation_storage: get_latest MISS campaign=%s",
            cleaned,
        )
        return None

    async def _fetch_by_campaign_name(
        self, campaign_name: str, client_code: str, auth_headers: dict = None
    ) -> dict | None:
        request = StorageReadRequest(
            storageName=self.STORAGE_NAME,
            appCode=self.APP_CODE,
            clientCode=client_code,
            filter=ComplexCondition(
                operator=LogicalOperator.AND,
                conditions=[
                    FilterCondition(
                        field="campaignName",
                        operator=FilterOperator.LIKE,
                        value=campaign_name,
                    ),
                    FilterCondition(
                        field="completed", operator=FilterOperator.IS_FALSE
                    ),
                ],
            ),
            sort=_SORT_UPDATED_AT_DESC,
            size=10,
        )
        response = await storage_service.read_page_storage(
            request, auth_headers=auth_headers
        )
        content = response.content or []
        if not content:
            # TODO: Remove this fallback once the scheduler has run in production and all
            # existing records have been migrated to camelCase keys. In production, the
            # old records will be deleted and the scheduler will re-populate with camelCase.
            request_snake = StorageReadRequest(
                storageName=self.STORAGE_NAME,
                appCode=self.APP_CODE,
                clientCode=client_code,
                filter=ComplexCondition(
                    operator=LogicalOperator.AND,
                    conditions=[
                        FilterCondition(
                            field="campaign_name",
                            operator=FilterOperator.LIKE,
                            value=campaign_name,
                        ),
                        FilterCondition(
                            field="completed", operator=FilterOperator.IS_FALSE
                        ),
                    ],
                ),
                sort=_SORT_UPDATED_AT_DESC,
                size=10,
            )
            response_snake = await storage_service.read_page_storage(
                request_snake, auth_headers=auth_headers
            )
            content = response_snake.content or []

        records = [r for r in content if isinstance(r, dict)]
        return records[0] if records else None

    async def _fetch_by_product_name(
        self, product_name: str, client_code: str, auth_headers: dict = None
    ) -> dict | None:
        request = StorageReadRequest(
            storageName=self.STORAGE_NAME,
            appCode=self.APP_CODE,
            clientCode=client_code,
            filter=ComplexCondition(
                operator=LogicalOperator.AND,
                conditions=[
                    FilterCondition(
                        field="productName",
                        operator=FilterOperator.LIKE,
                        value=product_name,
                    ),
                    FilterCondition(
                        field="completed", operator=FilterOperator.IS_FALSE
                    ),
                ],
            ),
            sort=_SORT_UPDATED_AT_DESC,
            size=10,
        )
        response = await storage_service.read_page_storage(
            request, auth_headers=auth_headers
        )
        content = response.content or []
        if not content:
            # TODO: Remove this fallback once the scheduler has run in production and all
            # existing records have been migrated to camelCase keys. In production, the
            # old records will be deleted and the scheduler will re-populate with camelCase.
            request_snake = StorageReadRequest(
                storageName=self.STORAGE_NAME,
                appCode=self.APP_CODE,
                clientCode=client_code,
                filter=ComplexCondition(
                    operator=LogicalOperator.AND,
                    conditions=[
                        FilterCondition(
                            field="product_name",
                            operator=FilterOperator.LIKE,
                            value=product_name,
                        ),
                        FilterCondition(
                            field="completed", operator=FilterOperator.IS_FALSE
                        ),
                    ],
                ),
                sort=_SORT_UPDATED_AT_DESC,
                size=10,
            )
            response_snake = await storage_service.read_page_storage(
                request_snake, auth_headers=auth_headers
            )
            content = response_snake.content or []

        records = [r for r in content if isinstance(r, dict)]
        return records[0] if records else None

    async def get_active_recommendations(
        self,
        campaign_id: str | None,
        client_code: str,
        auth_headers: dict,
    ) -> list[dict]:
        """Fetch all active recommendations for a given campaign, or ALL campaigns if campaign_id is None."""
        if campaign_id:
            record = await self._fetch_existing(
                campaign_id, client_code, auth_headers=auth_headers
            )
            return [record] if record else []

        # If no campaign_id is specified, fetch ALL non-completed recommendations for this clientCode!
        request = StorageReadRequest(
            storageName=self.STORAGE_NAME,
            appCode=self.APP_CODE,
            clientCode=client_code,
            filter=FilterCondition(field="completed", operator=FilterOperator.IS_FALSE),
            size=1000,  # TODO: add cursor-based pagination when clients have many campaigns
        )
        response = await storage_service.read_page_storage(
            request, auth_headers=auth_headers
        )
        content = response.content
        if not content:
            return []

        records = [r for r in content if isinstance(r, dict)]
        records.sort(
            key=lambda x: (
                x.get("updatedAt") or x.get("updated_at") or "1970-01-01T00:00:00Z"
            ),
            reverse=True,
        )
        return records


recommendation_storage_service = RecommendationStorageService()
