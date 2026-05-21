import logging
from app.core.tools.base import ToolDefinition, ToolResult, ToolParameter
from app.agents.adzump.adapters.meta.accounts import MetaAccountsAdapter
from app.agents.adzump.adapters.meta.age import MetaAgeAdapter
from app.agents.adzump.agents.optimization.meta.services.campaign_mapping_service import (
    campaign_mapping_service,
)

logger = logging.getLogger(__name__)


# Metric cleaning helpers
def _extract_performance_indicators(performance_row: dict) -> dict | None:
    """Extract only the strategic performance signals relevant for age targeting.

    Drops noisy nested API fields and returns only the clean scalars the LLM needs.
    """
    try:
        return {
            "age_range": performance_row.get("age", ""),
            "spend": round(float(performance_row.get("spend") or 0), 2),
            "impressions": int(performance_row.get("impressions") or 0),
            "reach": int(performance_row.get("reach") or 0),
            "unique_ctr": round(float(performance_row.get("unique_ctr") or 0), 4),
            "frequency": round(float(performance_row.get("frequency") or 0), 2),
            "cpc": round(float(performance_row.get("cpc") or 0), 2),
            "cpm": round(float(performance_row.get("cpm") or 0), 2),
        }
    except Exception as exception:
        logger.warning(f"Failed to extract performance indicators: {exception}")
        return None


# Tool functions
async def list_meta_business_accounts(parameters: dict, context: dict) -> ToolResult:
    """List Meta Business Manager accounts for the current client."""
    client_code = context.get("client_code")
    authentication_headers = context.get("headers", {})

    if not client_code:
        return ToolResult(
            success=False, error="Missing authentication context (client_code)"
        )

    try:
        accounts_adapter = MetaAccountsAdapter()
        business_accounts = await accounts_adapter.list_business_accounts(
            client_code, authentication_headers
        )
        business_account_names = "; ".join(
            [
                f"{business['name']} (id={business['id']})"
                for business in business_accounts
            ]
        )
        return ToolResult(
            success=True,
            summary=f"Found {len(business_accounts)} business accounts: {business_account_names}",
            data=business_accounts,
        )
    except Exception as exception:
        logger.error(f"Failed to list meta business accounts: {exception}")
        return ToolResult(success=False, error=str(exception))


async def list_meta_ad_accounts(parameters: dict, context: dict) -> ToolResult:
    """List Meta Ad Accounts under a specific Business ID."""
    business_id = parameters.get("business_id")
    client_code = context.get("client_code")
    authentication_headers = context.get("headers", {})

    if not business_id:
        return ToolResult(success=False, error="business_id is required")

    try:
        accounts_adapter = MetaAccountsAdapter()
        ad_accounts = await accounts_adapter.list_ad_accounts(
            business_id, client_code, authentication_headers
        )
        logger.info(f"Found {len(ad_accounts)} ad accounts for business {business_id}")
        ad_account_names = "; ".join(
            [f"{account['name']} (id={account['id']})" for account in ad_accounts]
        )
        return ToolResult(
            success=True,
            summary=f"Found {len(ad_accounts)} ad accounts for business {business_id}: {ad_account_names}",
            data=ad_accounts,
        )
    except Exception as exception:
        logger.error(f"Failed to list meta ad accounts: {exception}")
        return ToolResult(success=False, error=str(exception))


async def fetch_meta_age_metrics(parameters: dict, context: dict) -> ToolResult:
    """Fetch, clean, and group age performance metrics for a specific Ad Account."""
    ad_account_id = parameters.get("ad_account_id")
    client_code = context.get("client_code")
    authentication_headers = context.get("headers", {})

    if not ad_account_id:
        return ToolResult(success=False, error="ad_account_id is required")

    try:
        # Step 1: Fetch raw metrics from adapter
        age_adapter = MetaAgeAdapter()
        raw_performance_metrics = await age_adapter.fetch_age_metrics(
            ad_account_id, client_code, authentication_headers
        )

        logger.info(
            f"Received {len(raw_performance_metrics)} raw rows from Meta API for {ad_account_id}"
        )

        if not raw_performance_metrics:
            return ToolResult(
                success=True,
                summary=f"No age metrics found for account {ad_account_id}.",
                data={"adsets": []},
            )

        # Step 2: Get campaign mappings
        try:
            campaign_mappings = (
                await campaign_mapping_service.get_campaign_mapping_with_summary(
                    client_code, context
                )
            )
            logger.info(
                f"DEBUG: Known Campaign Mappings in DB: {list(campaign_mappings.keys())}"
            )
        except Exception as exception:
            logger.warning(f"Campaign mappings unavailable: {exception}")
            campaign_mappings = {}

        # Step 3: Filter and enrich
        linked_performance_metrics = []
        seen_campaigns = set()
        for performance_row in raw_performance_metrics:
            campaign_id = str(performance_row.get("campaign_id", ""))
            campaign_name = performance_row.get("campaign_name", "Unknown")

            if campaign_id not in seen_campaigns:
                logger.info(
                    f"DEBUG: Checking Meta Campaign: '{campaign_name}' (id={campaign_id})"
                )
                seen_campaigns.add(campaign_id)

            if campaign_id in campaign_mappings:
                mapping_data = campaign_mappings[campaign_id]
                performance_row["product_id"] = mapping_data.get("product_id", "")
                performance_row["product_context"] = mapping_data.get(
                    "summary", "No product info"
                )
                linked_performance_metrics.append(performance_row)
            else:
                logger.debug(
                    f"Campaign {campaign_id} SKIPPED (Not mapped to any product in DB)"
                )

        logger.info(
            f"DEBUG: Final Filtered Count: {len(linked_performance_metrics)} rows remain for analysis."
        )

        if not linked_performance_metrics:
            return ToolResult(
                success=True,
                summary=f"No campaigns linked to products found for account {ad_account_id}.",
                data={"adsets": []},
            )

        # Step 4: Group by adset
        adset_performance_groups: dict[str, dict] = {}
        for performance_row in linked_performance_metrics:
            adset_id = performance_row.get("adset_id", "")
            if not adset_id:
                continue

            if adset_id not in adset_performance_groups:
                adset_performance_groups[adset_id] = {
                    "adset_id": adset_id,
                    "adset_name": performance_row.get("adset_name", ""),
                    "campaign_id": str(performance_row.get("campaign_id", "")),
                    "campaign_name": performance_row.get("campaign_name", ""),
                    "campaign_objective": performance_row.get("objective", ""),
                    "product_id": performance_row.get("product_id", ""),
                    "product_context": performance_row.get(
                        "product_context", "No product info"
                    ),
                    "current_min": performance_row.get("current_min", 18),
                    "current_max": performance_row.get("current_max", 65),
                    "age_performance": [],
                }

            performance_indicators = _extract_performance_indicators(performance_row)
            if performance_indicators:
                adset_performance_groups[adset_id]["age_performance"].append(
                    performance_indicators
                )

        # Step 5: Finalize eligible adsets
        eligible_adsets = [
            adset_group
            for adset_group in adset_performance_groups.values()
            if len(adset_group["age_performance"]) >= 2
        ]

        if not eligible_adsets:
            return ToolResult(
                success=True,
                summary=f"Account {ad_account_id}: found {len(adset_performance_groups)} adsets but none had enough breakdown data.",
                data={"adsets": []},
            )

        return ToolResult(
            success=True,
            summary=f"Account {ad_account_id}: {len(eligible_adsets)} adsets ready for age analysis.",
            data={"adsets": eligible_adsets},
        )

    except Exception as exception:
        logger.error(f"Error fetching meta age metrics: {exception}")
        return ToolResult(success=False, error=str(exception))


async def get_campaign_mappings(parameters: dict, context: dict) -> ToolResult:
    """Get the mapping of Meta campaigns to internal products."""
    client_code = context.get("client_code")
    try:
        mappings = await campaign_mapping_service.get_campaign_mapping_with_summary(
            client_code, context
        )
        return ToolResult(
            success=True,
            summary=f"Found mappings for {len(mappings)} campaigns",
            data=mappings,
        )
    except Exception as exception:
        logger.error(f"Error getting campaign mappings: {exception}")
        return ToolResult(success=False, error=str(exception))


META_OPTIMIZATION_TOOLS = [
    ToolDefinition(
        name="list_meta_business_accounts",
        description="List all Meta Business Manager accounts the client has access to.",
        parameters=[],
        execute=list_meta_business_accounts,
    ),
    ToolDefinition(
        name="list_meta_ad_accounts",
        description="List all active Meta Ad Accounts under a specific Business Manager ID.",
        parameters=[
            ToolParameter(
                name="business_id",
                type="string",
                description="The ID of the Meta Business Manager account.",
                required=True,
            )
        ],
        execute=list_meta_ad_accounts,
    ),
    ToolDefinition(
        name="fetch_meta_age_metrics",
        description="Fetch cleaned and grouped age breakdown performance metrics for a Meta Ad Account.",
        parameters=[
            ToolParameter(
                name="ad_account_id",
                type="string",
                description="The Meta Ad Account ID (e.g., 'act_12345').",
                required=True,
            )
        ],
        execute=fetch_meta_age_metrics,
    ),
    ToolDefinition(
        name="get_campaign_mappings",
        description="Retrieve the mapping of Meta campaigns to internal products and business summaries.",
        parameters=[],
        execute=get_campaign_mappings,
    ),
]
