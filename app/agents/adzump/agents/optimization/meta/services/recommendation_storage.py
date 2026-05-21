import logging
from datetime import datetime, timezone

from app.agents.adzump.agents.optimization.meta.models import BaseCampaignRecommendation, MetaCampaignRecommendation
from app.agents.appbuilder.tools._shared import get_saas_client
from app.agents.adzump.tools._shared import build_ds_headers

logger = logging.getLogger(__name__)

READ_PAGE = "/api/core/function/execute/CoreServices.Storage/ReadPage"
CREATE = "/api/core/function/execute/CoreServices.Storage/Create"
UPDATE = "/api/core/function/execute/CoreServices.Storage/Update"


class RecommendationStorageService:
    STORAGE_NAME = "campaignSuggestions"
    APP_CODE = "marketingai"

    def _get_headers(self, context: dict) -> dict[str, str]:
        headers = build_ds_headers(context)
        headers["AppCode"] = self.APP_CODE
        headers["Content-Type"] = "application/json"
        return headers

    async def store(
        self, recommendation: BaseCampaignRecommendation, client_code: str, context: dict
    ) -> dict:
        """
        Store campaign recommendations in the database, fetch existing active recommendations
        for this campaign, merge new recommendations safely, create a new entry, and mark the
        previous suggestions as completed.
        """
        campaign_id = recommendation.campaign_id
        existing = await self._fetch_existing(campaign_id, client_code, context)

        base_fields = existing.get("fields", {}) if existing else None
        doc = self._build_recommendation(recommendation, base_fields)

        await self._create(doc, context)
        logger.info(f"recommendation_created: campaign_id={campaign_id}")

        if existing:
            existing_id = existing.get("_id") or existing.get("id")
            if existing_id:
                await self._mark_completed(existing_id, context)
                logger.info(
                    f"recommendation_previous_completed: campaign_id={campaign_id}, record_id={existing_id}"
                )

        return {"fields": doc["fields"]}

    async def _fetch_existing(self, campaign_id: str, client_code: str, context: dict) -> dict | None:
        """Read current uncompleted suggestions for this campaign from Nocode storage."""
        payload = {
            "storageName": self.STORAGE_NAME,
            "appCode": self.APP_CODE,
            "clientCode": client_code,
            "filter": {
                "operator": "AND",
                "conditions": [
                    {"field": "campaign_id", "value": campaign_id},
                    {"field": "completed", "value": False},
                ],
            },
            "size": 1,
        }

        res = await get_saas_client().post(
            READ_PAGE,
            headers=self._get_headers(context),
            json=payload,
        )
        if not res.success or not res.data:
            return None

        # Unwrap Nocode storage gateway payload response envelope
        data = res.data
        if isinstance(data, list) and data:
            data = data[0]
            
        for _ in range(2):
            if isinstance(data, dict) and "result" in data:
                data = data["result"]

        if isinstance(data, dict) and "content" in data:
            content = data["content"]
            record = content[0] if isinstance(content, list) and content else None
        else:
            record = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else None)

        if not record:
            return None

        # Nocode wraps custom fields inside a nested "data" sub-object.
        # We extract them and merge the top-level "_id" into a flat dictionary.
        record_id = record.get("_id") or record.get("id")
        fields = record.get("data") if isinstance(record.get("data"), dict) else record
        
        result_dict = dict(fields)
        if record_id:
            result_dict["_id"] = record_id
        return result_dict

    def _build_recommendation(
        self, rec: BaseCampaignRecommendation, base_fields: dict | None
    ) -> dict:
        """Construct the recommendation document dict to write into the database."""
        fields = self._merge_fields(rec, base_fields)
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

    def _merge_fields(
        self, rec: BaseCampaignRecommendation, base_fields: dict | None
    ) -> dict:
        """Safely merge incoming recommendation items, preserving prior non-conflicting fields."""
        fields = dict(base_fields) if base_fields else {}
        rec_fields = rec.fields.model_dump(exclude_none=True)
        for key, new_items in rec_fields.items():
            for item in new_items:
                item["applied"] = False
            existing = fields.get(key, [])
            
            # Keywords require origin-based merging to keep distinct sources intact
            if key in ("keywords", "negativeKeywords"):
                origins = {
                    item.get("origin") for item in new_items if item.get("origin")
                }
                kept = [item for item in existing if item.get("origin") not in origins]
                fields[key] = kept + new_items
            else:
                # For non-keyword properties (e.g. age range), overwrite completely
                fields[key] = new_items
        return fields

    async def _create(self, doc: dict, context: dict):
        """Create a suggestion document record."""
        payload = {
            "storageName": self.STORAGE_NAME,
            "appCode": self.APP_CODE,
            "dataObject": doc,
        }
        res = await get_saas_client().post(
            CREATE,
            headers=self._get_headers(context),
            json=payload,
        )
        if not res.success:
            raise Exception(f"Failed to create suggestion in Nocode storage: {res.error}")

    async def _mark_completed(self, record_id: str, context: dict):
        """Update suggestion record to mark it completed."""
        payload = {
            "storageName": self.STORAGE_NAME,
            "appCode": self.APP_CODE,
            "dataObjectId": record_id,
            "dataObject": {"completed": True},
            "isPartial": True,
        }
        res = await get_saas_client().post(
            UPDATE,
            headers=self._get_headers(context),
            json=payload,
        )
        if not res.success:
            logger.warning(f"Failed to mark suggestion {record_id} completed: {res.error}")

    async def fetch_all_active_recommendations(
        self, client_code: str, context: dict
    ) -> list[MetaCampaignRecommendation]:
        """
        Fetch all active (uncompleted) recommendations for this client.
        """
        payload = {
            "storageName": self.STORAGE_NAME,
            "appCode": self.APP_CODE,
            "clientCode": client_code,
            "filter": {
                "field": "completed",
                "value": False
            },
            "size": 200,
        }

        res = await get_saas_client().post(
            READ_PAGE,
            headers=self._get_headers(context),
            json=payload,
        )
        if not res.success or not res.data:
            return []

        data = res.data
        if isinstance(data, list) and data:
            data = data[0]
            
        for _ in range(2):
            if isinstance(data, dict) and "result" in data:
                data = data["result"]

        if isinstance(data, dict) and "content" in data:
            records = data["content"] or []
        else:
            records = data if isinstance(data, list) else []

        recommendations = []
        for record in records:
            if not isinstance(record, dict):
                continue
            record_id = record.get("_id") or record.get("id")
            fields = record.get("data") if isinstance(record.get("data"), dict) else record
            
            result_dict = dict(fields)
            if record_id:
                result_dict["_id"] = record_id
            
            if not result_dict.get("campaign_id") or not result_dict.get("account_id") or not result_dict.get("parent_account_id"):
                logger.debug(f"Skipping legacy suggestion record missing required IDs: {record_id}")
                continue
            
            try:
                rec_id = result_dict.get("_id") or result_dict.get("id")
                rec = MetaCampaignRecommendation(
                    _id=rec_id,
                    id=rec_id,
                    platform=result_dict.get("platform", "META"),
                    parent_account_id=result_dict.get("parent_account_id", ""),
                    account_id=result_dict.get("account_id", ""),
                    product_id=result_dict.get("product_id"),
                    campaign_id=result_dict.get("campaign_id", ""),
                    campaign_name=result_dict.get("campaign_name", ""),
                    campaign_type=result_dict.get("campaign_type", ""),
                    completed=result_dict.get("completed", False),
                    fields=result_dict.get("fields", {}),
                )
                recommendations.append(rec)
            except Exception as e:
                logger.debug(f"Failed to parse active recommendation (legacy format): {e}")

        return recommendations

    async def fetch_active_recommendation(
        self, campaign_id: str, client_code: str, context: dict
    ) -> MetaCampaignRecommendation | None:
        """
        Fetch active (uncompleted) recommendation for this campaign.
        """
        existing = await self._fetch_existing(campaign_id, client_code, context)
        if not existing:
            return None
            
        if not existing.get("campaign_id") or not existing.get("account_id") or not existing.get("parent_account_id"):
            logger.debug(f"Skipping legacy cached recommendation missing required IDs: {existing.get('_id')}")
            return None
            
        try:
            rec_id = existing.get("_id") or existing.get("id")
            return MetaCampaignRecommendation(
                _id=rec_id,
                id=rec_id,
                platform=existing.get("platform", "META"),
                parent_account_id=existing.get("parent_account_id", ""),
                account_id=existing.get("account_id", ""),
                product_id=existing.get("product_id"),
                campaign_id=existing.get("campaign_id", ""),
                campaign_name=existing.get("campaign_name", ""),
                campaign_type=existing.get("campaign_type", ""),
                completed=existing.get("completed", False),
                fields=existing.get("fields", {}),
            )
        except Exception as e:
            logger.debug(f"Failed to parse cached recommendation (legacy format): {e}")
            return None


recommendation_storage_service = RecommendationStorageService()
