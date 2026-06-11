"""Read stored campaign recommendations from storage."""

from __future__ import annotations

import logging


from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.agents.optimization.models import CampaignRecommendation

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
        from pydantic import TypeAdapter
        stored = TypeAdapter(CampaignRecommendation).validate_python(stored_dict) if stored_dict else None
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

    fields = stored.fields
    summary_lines = [
        f"Campaign: {stored.campaign_name} ({stored.campaign_id})",
        f"Platform: {stored.platform}",
        f"Source: {stored.source}",
        f"Generated: {stored.generated_at}",
        "",
    ]

    if fields.conversion_health:
        ch = fields.conversion_health
        summary_lines.append(
            f"Conversion Health: {ch.health_label} ({ch.health_score}/100)"
        )
        failing = [c for c in ch.checks if not c.passed]
        if failing:
            for c in failing:
                summary_lines.append(f"  [{c.severity.upper()}] {c.title}")
        if ch.fix_cards:
            summary_lines.append(
                f"  {len(ch.fix_cards)} fix card(s) available "
                f"({sum(1 for fc in ch.fix_cards if fc.can_auto_apply)} auto-applicable)"
            )
        summary_lines.append("")

    if fields.budget_bidding:
        bb = fields.budget_bidding
        summary_lines.append("Budget & Bidding:")
        summary_lines.append(f"  Current strategy: {bb.current_strategy}")
        if bb.bidding_rec_type:
            summary_lines.append(
                f"  Recommended: {bb.bidding_rec_type} "
                f"(confidence: {bb.bidding_confidence})"
            )
            summary_lines.append(f"  Rationale: {bb.bidding_rec_rationale}")
        if bb.recommended_budget:
            summary_lines.append(
                f"  Budget: {bb.current_budget:.2f} -> {bb.recommended_budget:.2f} "
                f"(confidence: {bb.budget_confidence})"
            )
        if bb.learning_phase_warning:
            summary_lines.append(f"  {bb.learning_phase_warning}")
        if bb.blocking_issues:
            for issue in bb.blocking_issues:
                summary_lines.append(f"  {issue}")
        summary_lines.append("")

    if fields.keywords:
        kws = fields.keywords
        pause_recs = [k for k in kws if k.recommendation == "PAUSE"]
        add_recs = [k for k in kws if k.recommendation == "ADD"]
        summary_lines.append("Keywords:")
        summary_lines.append(f"  {len(pause_recs)} PAUSE recommendation(s)")
        summary_lines.append(f"  {len(add_recs)} ADD recommendation(s)")
        if pause_recs:
            for k in pause_recs[:3]:
                summary_lines.append(f"    PAUSE '{k.text}' — {k.reason}")
            if len(pause_recs) > 3:
                summary_lines.append(f"    ... and {len(pause_recs) - 3} more")
        if add_recs:
            for k in add_recs[:3]:
                summary_lines.append(
                    f"    ADD '{k.text}' ({k.match_type}) — score: {k.score or 'N/A'}"
                )
            if len(add_recs) > 3:
                summary_lines.append(f"    ... and {len(add_recs) - 3} more")
        summary_lines.append("")

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
