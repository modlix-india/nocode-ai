"""Read stored campaign recommendations from storage."""

from __future__ import annotations
from pydantic import TypeAdapter

import logging


from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.recommendations.models import CampaignRecommendation

logger = logging.getLogger(__name__)


async def _get_recommendations(params: dict, context: dict) -> ToolResult:
    """Fetch stored campaign recommendations and build a summary."""
    campaign_id = str(params.get("campaign_id") or "").strip()
    client_code = context.get("client_code", "")

    headers = context.get("headers", {})

    if not campaign_id:
        return ToolResult(
            success=False,
            error="campaign_id is required",
        )

    try:
        from app.agents.adzump.services.recommendation_storage import (
            recommendation_storage_service,
        )

        stored_dict = await recommendation_storage_service.get_latest(
            campaign_id=campaign_id,
            client_code=client_code,
            auth_headers=headers,
        )
        stored = (
            TypeAdapter(CampaignRecommendation).validate_python(stored_dict)
            if stored_dict
            else None
        )
    except Exception as e:
        logger.exception(
            "get_recommendations: storage read failed campaign=%s", campaign_id
        )
        return ToolResult(
            success=False,
            error=f"Storage read failed: {type(e).__name__}: {e}",
        )

    if not stored:
        return ToolResult(
            success=True,
            data={"found": False, "campaign_id": campaign_id},
            summary=(
                f"No stored recommendations found for campaign {campaign_id}. "
                "Ask the user if they want a fresh analysis."
            ),
        )

    summary_lines = [
        f"Campaign: {stored.campaign_name} ({stored.campaign_id})",
        f"Platform: {stored.platform}",
        f"Source: {stored.source}",
        f"Generated: {stored.generated_at}",
        "",
    ]

    # Platform-agnostic summary: this tool is shared by the optimization agent
    # across ALL platforms, so it must not reach into Google-specific fields
    # (conversion_health / budget_bidding / keywords) directly. Each platform
    # provider renders its own fields via summarize_fields — same delegation
    # used by tools/optimize.py:_summarize_stored_recommendation.
    try:
        from app.agents.adzump.agents.optimization.platform_registry import (
            get_provider,
        )

        provider = get_provider(stored.platform)
        if provider and stored.fields:
            summary_lines.extend(provider.summarize_fields(stored.fields))
    except Exception:
        logger.warning(
            "get_recommendations: summarize_fields failed platform=%s campaign=%s",
            stored.platform,
            campaign_id,
            exc_info=True,
        )

    return ToolResult(
        success=True,
        data={
            "found": True,
            "recommendation": stored.model_dump(),
        },
        summary="\n".join(summary_lines),
    )


# REQUIRED: Analyst sub-agent uses this for fast, zero-cost cache baseline checks
# from storage before triggering expensive Google/Meta real-time API scans.
get_recommendations = ToolDefinition(
    name="get_recommendations",
    description=(
        "Fetch stored recommendations for a campaign from the last scheduler run. "
        "Call this FIRST when the user asks to see recommendations. "
        "Returns immediately from storage — no API calls. "
        "If results are found, present them to the user directly. "
        "If not found, ask if the user wants a fresh analysis."
    ),
    display_name="Get Recommendations",
    parameters=[
        ToolParameter(
            name="campaign_id",
            type="string",
            description="Google Ads or Meta campaign ID to fetch recommendations for.",
            required=True,
        ),
    ],
    execute=_get_recommendations,
)
