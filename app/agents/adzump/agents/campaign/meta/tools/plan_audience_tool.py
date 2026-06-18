# app/agents/adzump/agents/campaign/meta/tools/plan_audience_tool.py
"""Meta Campaign Audience Planning tool."""

import logging
from typing import Any, List

from app.core.tools.base import ToolDefinition, ToolResult
from app.agents.adzump.agents.campaign.meta.audience_planner_agent import (
    MetaAudiencePlannerAgent,
)
from app.agents.adzump.agents.campaign.meta.models import BusinessProfileInput

logger = logging.getLogger(__name__)

# Mapping from normalized gender key -> (stored campaign_spec array, display label)
GENDER_MAPPINGS = {
    "male": (["MALE"], "Male"),
    "female": (["FEMALE"], "Female"),
    "all": (["MALE", "FEMALE"], "Male and Female"),
}



async def _plan_meta_audience(
    params: dict[str, Any], context: dict[str, Any]
) -> ToolResult:
    """Analyze business data and campaign objectives to suggest target demographics.

    This tool runs the MetaAudiencePlannerAgent to determine recommended age brackets
    and gender settings for a new Meta Ads campaign. It streams the suggestion to the user
    for confirmation and does not mutate any stored states.
    """
    session_ctx = context.get("session_context") or {}
    product_data = session_ctx.get("product_data") or {}
    product_profile = session_ctx.get("product_profile") or {}
    campaign_spec = session_ctx.get("campaign_spec") or {}

    # Extract inputs dynamically from the session context
    business_type = product_data.get("business_type") or "Local Business"

    # Fallback cascade for business summary/description
    business_summary = (
        product_profile.get("summary")
        or product_data.get("summary")
        or "No summary available."
    )

    # Clean and parse locations list
    raw_location = (
        campaign_spec.get("location")
        or product_data.get("location")
        or product_data.get("suggested_locations")
    )
    locations = []
    if raw_location:
        locations = [raw_location] if isinstance(raw_location, str) else list(raw_location)
    locations = [str(loc).strip() for loc in locations if loc]

    # Extract and normalize pricing information
    prices: List[str] = []
    pricing_data = product_data.get("pricing")
    if pricing_data:
        if isinstance(pricing_data, list):
            prices = [str(p).strip() for p in pricing_data if p]
        else:
            prices = [str(pricing_data).strip()]

    campaign_objective = campaign_spec.get("campaign_objective") or "OUTCOME_LEADS"

    # Build input payload
    profile_input = BusinessProfileInput(
        businessType=business_type,
        businessSummary=business_summary,
        locations=locations if locations else None,
        prices=prices if prices else None,
        campaignObjective=campaign_objective,
    )

    try:
        # Run agent
        agent = MetaAudiencePlannerAgent()
        audience_plan = await agent.plan_audience(profile_input)
    except Exception as e:
        logger.exception("Failed to generate audience suggestions: %s", e)
        return ToolResult(
            success=False,
            error=f"Demographic planning failed: {str(e)}",
        )

    rec = audience_plan.recommendation

    # ── 1. MAP AND STORE TARGETING TO SESSION CONTEXT ──
    campaign_spec = session_ctx.setdefault("campaign_spec", {})
    campaign_spec["age_min"] = rec.ageMin
    campaign_spec["age_max"] = rec.ageMax

    stored_genders, display_gender = GENDER_MAPPINGS.get(rec.gender, GENDER_MAPPINGS["all"])
    campaign_spec["gender"] = stored_genders

    # ── 2. CONSTRUCT CRAFT SIDE PANEL BLOCKS (COLORFUL CALLOUTS) ──
    craft_blocks = [
        {"type": "badge", "label": "Meta Ads Audience Plan"},
        {"type": "heading", "text": "Targeting Settings", "level": 2},
    ]

    age_range = (
        f"{rec.ageMin} - {rec.ageMax} years"
        if rec.ageMin and rec.ageMax
        else "Broad targeting"
    )
    craft_blocks.append(
        {"type": "callout", "text": f"🎯 Age Range: {age_range}", "variant": "info"}
    )
    craft_blocks.append(
        {
            "type": "callout",
            "text": f"👥 Gender: {display_gender}",
            "variant": "success",
        }
    )
    craft_blocks.append(
        {
            "type": "callout",
            "text": f"⚡ Confidence: {int(audience_plan.confidence * 100)}%",
            "variant": "warning",
        }
    )

    # ── 3. CONSTRUCT CHAT EXPLANATION TEXT ──
    chat_markdown = "🎯 **Meta Ads Audience Plan ready!** I have loaded the targeting recommendations into the side panel.\n\n"

    # Rationale & Targeting Insights (Limit to top 3 key insights)
    if audience_plan.reasoning:
        chat_markdown += "#### 💡 Strategic Rationale & Targeting Insights\n"
        for reason in audience_plan.reasoning[:3]:
            chat_markdown += f"- {reason}\n"
        chat_markdown += "\n"

    # ── 4. EMIT BOTH TO THE USER STREAM ──
    stream = context.get("event_stream")
    if stream is not None:
        # Retrieve or generate a dedicated audience craft panel ID to spawn a new panel
        craft_id = session_ctx.get("meta_audience_craft_id")
        if not craft_id:
            import uuid
            craft_id = f"meta_audience_{uuid.uuid4().hex[:8]}"
            session_ctx["meta_audience_craft_id"] = craft_id

        await stream.emit_craft(
            craft_id=craft_id,
            title="Meta Ads Audience Plan",
            blocks=craft_blocks,
            append=False,
        )
        await stream.emit_text(chat_markdown)

    return ToolResult(
        success=True,
        data=audience_plan.model_dump(),
        summary=(
            f"Successfully planned audience suggestions "
            f"(Confidence: {int(audience_plan.confidence * 100)}%)."
        ),
    )


# Define the chat tool schema
plan_meta_audience = ToolDefinition(
    name="plan_meta_audience",
    description=(
        "Analyze a business profile (from scraping data) and campaign objective "
        "to suggest initial demographic targeting options (age limits, gender settings) "
        "during campaign creation. Does NOT save to the active campaign spec."
    ),
    display_name="Plan Meta Audience",
    parameters=[],
    execute=_plan_meta_audience,
)
