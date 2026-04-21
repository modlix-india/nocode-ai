"""Campaign data tool — stores campaign fields in session context.

Provides a single tool that the LLM calls to persist campaign data
(platform, budget, duration, etc.) into session.context. This makes
slot-filling explicit and auditable rather than relying on the LLM
to manage state implicitly.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

# Fields that can be set via this tool.
# Only fields actually consumed downstream (display, account routing,
# future publish tools). Transient inputs like leads_target are passed
# directly to the tool that needs them, not persisted here.
ALLOWED_FIELDS = {
    "platform",
    "duration",
    "budget",
    "google_account",
    "meta_account",
}


async def _set_campaign_data(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Store campaign data fields in session context."""
    session_ctx = context.get("session_context")
    if session_ctx is None:
        return ToolResult(success=False, error="No session context available.")

    campaign = session_ctx.setdefault("campaign_data", {})

    updated: list[str] = []
    for key in ALLOWED_FIELDS:
        value = params.get(key)
        if value is not None and value != "":
            campaign[key] = value
            updated.append(key)

    if not updated:
        return ToolResult(
            success=False,
            error="No valid fields provided. Allowed fields: " + ", ".join(sorted(ALLOWED_FIELDS)),
        )

    logger.info("campaign_data_updated: fields=%s", updated)
    return ToolResult(
        success=True,
        summary=f"Campaign data updated: {', '.join(updated)}",
    )


set_campaign_data = ToolDefinition(
    name="set_campaign_data",
    description=(
        "Store campaign configuration fields. Call this whenever the user provides "
        "campaign details (platform, budget, duration, or account selection). "
        "You can set multiple fields in a single call."
    ),
    display_name="Update Campaign Data",
    parameters=[
        ToolParameter(
            name="platform",
            type="string",
            description="Advertising platform: 'Google Ads' or 'Meta'",
            required=False,
            enum=["Google Ads", "Meta"],
        ),
        ToolParameter(
            name="duration",
            type="string",
            description="Campaign duration (e.g., '30 days', '3 months')",
            required=False,
        ),
        ToolParameter(
            name="budget",
            type="string",
            description="Daily budget (e.g., '$50/day')",
            required=False,
        ),
        ToolParameter(
            name="google_account",
            type="string",
            description="Selected Google Ads account ID (customer_id)",
            required=False,
        ),
        ToolParameter(
            name="meta_account",
            type="string",
            description="Selected Meta ad account ID",
            required=False,
        ),
    ],
    execute=_set_campaign_data,
)

async def _predict_budget(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Call the ds budget prediction API to estimate daily budget from lead target."""
    leads = params.get("leads_target")
    if not leads:
        return ToolResult(success=False, error="leads_target is required.")

    try:
        leads = int(leads)
    except (ValueError, TypeError):
        return ToolResult(success=False, error=f"leads_target must be a number, got: {leads}")

    session_ctx = context.get("session_context") or {}
    campaign = session_ctx.get("campaign_data", {})
    duration_str = campaign.get("duration", "30 days")

    # Parse duration to days.
    import re
    days_match = re.search(r"(\d+)\s*(day|month)", duration_str.lower())
    if days_match:
        num = int(days_match.group(1))
        unit = days_match.group(2)
        duration_days = num * 30 if "month" in unit else num
    else:
        duration_days = 30

    # Call ds budget prediction API.
    try:
        from app.agents.adzump.tools._shared import get_ds_client, build_ds_headers

        client = get_ds_client()
        headers = build_ds_headers(context)

        result = await client.post(
            "/api/ds/prediction/budget/",
            headers=headers,
            json={
                "conversions": leads,
                "duration_days": duration_days,
            },
        )

        if not result.success:
            return ToolResult(
                success=False,
                error=f"Budget prediction failed: {result.error or 'unknown'}",
            )

        data = result.data or {}
        suggested_budget = data.get("suggested_budget", 0)
        base_cost = data.get("base_cost_prediction", 0)

        # Convert total to daily.
        daily_budget = round(suggested_budget / duration_days) if duration_days > 0 else suggested_budget

        # Round nicely.
        if daily_budget <= 1000:
            daily_budget = round(daily_budget / 100) * 100
        else:
            daily_budget = round(daily_budget / 1000) * 1000

        if daily_budget < 100:
            daily_budget = 100

        summary = (
            f"Based on {leads} target leads over {duration_days} days:\n"
            f"Recommended daily budget: ₹{daily_budget:,}/day\n"
            f"Total estimated cost: ₹{suggested_budget:,}\n\n"
            "This includes a 20% safety buffer. The user can confirm or adjust."
        )

        logger.info("predict_budget: leads=%d duration=%d daily=₹%d total=₹%d",
                     leads, duration_days, daily_budget, suggested_budget)

        return ToolResult(
            success=True,
            data={
                "daily_budget": daily_budget,
                "total_budget": suggested_budget,
                "base_cost": base_cost,
                "duration_days": duration_days,
                "leads_target": leads,
            },
            summary=summary,
        )

    except Exception as e:
        logger.warning("predict_budget failed: %s: %s", type(e).__name__, str(e)[:200])
        return ToolResult(
            success=False,
            error=f"Budget prediction unavailable: {e}",
        )


predict_budget = ToolDefinition(
    name="predict_budget",
    description=(
        "Estimate a recommended daily budget based on target lead count and campaign "
        "duration. Calls the ML prediction model. Use this when the user provides a "
        "lead target for Google Ads campaigns. Returns a suggested daily budget that "
        "the user can confirm or adjust."
    ),
    display_name="Predict Budget",
    parameters=[
        ToolParameter(
            name="leads_target",
            type="string",
            description="Target number of leads (e.g., '50', '100', '200')",
            required=True,
        ),
    ],
    execute=_predict_budget,
)


CAMPAIGN_DATA_TOOLS = [set_campaign_data, predict_budget]
