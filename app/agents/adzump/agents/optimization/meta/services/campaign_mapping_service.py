import logging

from app.agents.adzump.services.business_storage import (
    storage_read,
    STORAGE_NAME,
    APP_CODE,
)
from app.agents.adzump.agents.product.models import StorageReadRequest

logger = logging.getLogger(__name__)


class CampaignMappingService:
    """Service for mapping Meta/Google campaigns to internal products and business data."""

    def __init__(self) -> None:
        pass

    async def get_campaign_product_mapping(
        self, client_code: str, context: dict
    ) -> dict[str, str]:
        """
        Returns a mapping of campaign_id to product_id.
        """
        storage_request = StorageReadRequest(
            storageName=STORAGE_NAME,
            appCode=APP_CODE,
            clientCode=client_code,
            size=200,
        )
        storage_records = await storage_read(storage_request, context)
        if not storage_records:
            logger.info(f"No storage records found for client {client_code}")
            return {}

        return self._build_campaign_id_mapping(storage_records)

    async def get_campaign_mapping_with_summary(
        self, client_code: str, context: dict
    ) -> dict[str, dict]:
        """
        Returns a mapping of campaign_id to a dictionary containing product metadata.
        """
        storage_request = StorageReadRequest(
            storageName=STORAGE_NAME,
            appCode=APP_CODE,
            clientCode=client_code,
            size=200,
        )
        storage_records = await storage_read(storage_request, context)
        if not storage_records:
            logger.info(f"No storage records found for client {client_code}")
            return {}

        return self._build_detailed_mapping(storage_records)

    def _build_campaign_id_mapping(self, storage_records: list) -> dict[str, str]:
        """Builds a flat campaign_id -> product_id dictionary (rugged version)."""
        campaign_mapping = {}
        for storage_record in storage_records:
            product_id = storage_record.get("_id")
            # Storage records often nest data under a 'data' key
            record_content = storage_record.get("data", storage_record)

            #  Check for 'campaigns' list
            for campaign in record_content.get("campaigns", []):
                campaign_id = str(campaign.get("campaignId", ""))
                if campaign_id:
                    campaign_mapping[campaign_id] = product_id

        return campaign_mapping

    def _build_detailed_mapping(self, storage_records: list) -> dict[str, dict]:
        """Builds a detailed campaign_id -> product_metadata dictionary (rugged version)."""
        detailed_mapping = {}
        for storage_record in storage_records:
            product_id = storage_record.get("_id")
            record_content = storage_record.get("data", storage_record)

            final_summary = record_content.get(
                "finalSummary", record_content.get("summary", "")
            )
            business_url = record_content.get("businessUrl", "")
            product_name = record_content.get(
                "productName", record_content.get("businessName", "")
            )

            # Helper to add to mapping
            def add_to_mapping(c_id: str, c_name: str = "", platform: str = "META"):
                if c_id and c_id not in detailed_mapping:
                    detailed_mapping[c_id] = {
                        "product_id": product_id,
                        "product_name": product_name,
                        "campaign_id": c_id,
                        "campaign_name": c_name,
                        "summary": final_summary,
                        "business_url": business_url,
                        "platform": platform,
                    }

            #  Check for 'campaigns' list
            for campaign in record_content.get("campaigns", []):
                c_id = str(campaign.get("campaignId", ""))
                c_name = str(campaign.get("campaignName", ""))
                platform = str(campaign.get("platform", "META")).upper()
                add_to_mapping(c_id, c_name, platform)

        return detailed_mapping


campaign_mapping_service = CampaignMappingService()


if __name__ == "__main__":
    import asyncio
    import os
    from dotenv import load_dotenv

    load_dotenv()

    async def run_test():
        client_code = os.getenv("CLIENT_CODE")
        authorization_token = os.getenv("AUTH_TOKEN")

        if not client_code or not authorization_token:
            logger.error("CLIENT_CODE and AUTH_TOKEN must be set in .env")
            return

        test_context = {
            "client_code": client_code,
            "auth_headers": {"Authorization": f"Bearer {authorization_token}"},
        }

        mapping_service = CampaignMappingService()
        logger.info(f"Testing storage read for client: {client_code}...")

        flat_mapping = await mapping_service.get_campaign_product_mapping(
            client_code, test_context
        )
        logger.info(f"Campaign -> Product ID Mapping: {flat_mapping}")

        full_mapping = await mapping_service.get_campaign_mapping_with_summary(
            client_code, test_context
        )
        print(full_mapping)
        logger.info(f"Detailed Mapping: {full_mapping}")

    asyncio.run(run_test())
