"""optimize tool — exposed to AdzumpAgent (the main chat agent).

Bridge between the main chat interface and OptimizationAgent. Thin dispatcher:
builds a typed ``OptimizationRequest`` and routes to the storage-read flow (no LLM
cost) or the fresh-analysis sub-agent flow — both implemented in
``_optimize_flows.py``. Registered in app/agents/adzump/tools/registry.py.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.agents.adzump.services.recommendation_storage import (
    recommendation_storage_service,
)
from app.agents.adzump.tools._optimize_flows import (
    OptimizationRequest,
    _fresh_analysis,
    _serve_from_storage,
)
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


_COOLDOWN_HOURS: dict[str, int] = {
    "scheduler": 20,        # nightly scheduler ran; ad data takes ~24h to settle
    "user_requested": 4,    # user ran a scan recently; data won't have changed meaningfully
}
_DEFAULT_COOLDOWN_HOURS = 20


def _cooldown_decision(
    generated_at_raw: str, source: str, now: datetime
) -> tuple[bool, float]:
    """Pure cooldown policy. Returns ``(within_cooldown, age_hours)``:
    ``within_cooldown`` is True when a stored rec is recent enough that a fresh
    scan would just repeat it. An empty/unparseable timestamp → ``(False, 0.0)``
    (proceed with a fresh analysis). No I/O — testable in isolation."""
    cooldown_hours = _COOLDOWN_HOURS.get(source, _DEFAULT_COOLDOWN_HOURS)
    if not generated_at_raw:
        return (False, 0.0)
    try:
        generated_dt = datetime.fromisoformat(generated_at_raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return (False, 0.0)
    age_hours = (now - generated_dt).total_seconds() / 3600
    return (age_hours < cooldown_hours, age_hours)


# Entry point — dispatch to storage read or sub-agent analysis based on params
async def _optimize(params: dict, context: dict) -> ToolResult:
    """Entry point — build the request, then route to storage or fresh analysis."""
    req = OptimizationRequest.from_tool_call(params, context)

    logger.info(
        "optimize: ENTRY campaign_id=%s fresh=%s platform=%s question=%s",
        req.campaign_id or "(all)",
        req.fresh,
        req.platform or "(any)",
        bool(req.question),
    )

    # Preserve the campaign across turns so the next agent loop keeps context.
    if req.session and req.campaign_id:
        req.session.context["active_campaign_id"] = req.campaign_id

    # Default path: serve stored recommendations (all campaigns, or one campaign's
    # pre-calculated nightly report) without any API/LLM cost.
    if not req.fresh:
        return await _serve_from_storage(req)

    if not req.campaign_id:
        logger.warning("optimize: fresh analysis requested but no campaign_id provided")
        return ToolResult(
            success=False,
            error=(
                "I need to know which campaign to analyze. "
                "Could you please provide the campaign ID or name?"
            ),
        )

    if not req.session or not req.auth or not req.event_stream:
        logger.error(
            "optimize: fresh analysis BLOCKED — missing session/auth/stream "
            "session=%s auth=%s stream=%s",
            bool(req.session),
            bool(req.auth),
            bool(req.event_stream),
        )
        return ToolResult(
            success=False,
            error=(
                "Something went wrong setting up the analysis session. "
                "Please refresh the page and try again."
            ),
        )

    # Cooldown gate — if a recent recommendation already exists, serve it instead of
    # spawning a sub-agent. Ad platform data takes ~24h to settle, so re-running sooner
    # produces near-identical results at unnecessary LLM cost. Bypassed by force_refresh.
    if not req.force_refresh:
        try:
            stored_for_cooldown = await recommendation_storage_service.get_latest(
                campaign_id=req.campaign_id,
                client_code=req.client_code,
                auth_headers=req.headers or {},
            )
        except Exception:
            logger.warning(
                "optimize: cooldown gate lookup failed; proceeding with fresh analysis",
                exc_info=True,
            )
            stored_for_cooldown = None

        if stored_for_cooldown:
            generated_at_raw = (
                stored_for_cooldown.get("generatedAt")
                or stored_for_cooldown.get("generated_at", "")
            )
            source = stored_for_cooldown.get("source", "scheduler")
            within_cooldown, age_hours = _cooldown_decision(
                generated_at_raw, source, datetime.now(timezone.utc)
            )

            if within_cooldown:
                logger.info(
                    "optimize: COOLDOWN GATE fired campaign=%s age_hours=%.1f "
                    "source=%s — serving stored rec",
                    req.campaign_id,
                    age_hours,
                    source,
                )
                storage_result = await _serve_from_storage(
                    req, prefetched_latest=stored_for_cooldown
                )
                age_str = (
                    f"{age_hours:.0f}h ago"
                    if age_hours >= 1
                    else f"{age_hours * 60:.0f}m ago"
                )
                cooldown_note = (
                    f"\n\n[COOLDOWN]: This analysis ran {age_str} (source: {source}). "
                    f"Ad platform data takes up to 24h to settle, so a new scan now "
                    f"would likely produce the same results. Present the stored "
                    f"recommendations to the user and explain the data lag. "
                    f"If they still want a fresh scan, they can ask you to 'force refresh'."
                )
                return ToolResult(
                    success=storage_result.success,
                    data=storage_result.data,
                    summary=(storage_result.summary or "") + cooldown_note,
                    error=storage_result.error,
                )

    return await _fresh_analysis(req)


optimize = ToolDefinition(
    name="optimize",
    description=(
        "Get campaign optimization recommendations from Google Ads or Meta. "
        "By default (fresh=False), fetches stored recommendations from the last scheduler run. "
        "Set fresh=True ONLY when the user explicitly requests a live or fresh scan. "
        "Set question to route a specific optimization question to the analyst. "
        "CRITICAL: DO NOT trigger multiple parallel tool calls for different platforms! "
        "Instead, omit the platform parameter entirely to load and display campaigns from BOTH "
        "platforms simultaneously inside a single unified dashboard."
    ),
    display_name="Campaign Optimizer",
    parameters=[
        ToolParameter(
            name="campaign_id",
            type="string",
            description=(
                "Campaign ID to get recommendations for. Omit this only when "
                "the user asks to list all stored recommendations for the client."
            ),
            required=False,
        ),
        ToolParameter(
            name="platform",
            type="string",
            description=(
                "Filter stored recommendations by platform ('GOOGLE' or 'META'). "
                "Omit this parameter to retrieve and display BOTH platforms concurrently in a single dashboard."
            ),
            required=False,
        ),
        ToolParameter(
            name="fresh",
            type="boolean",
            description=(
                "CRITICAL: Set True ONLY when the user explicitly requests a live/fresh scan "
                "using words like 'fresh scan', 'live scan', 'run live scan', 'refresh', or clicked the 'Yes, Run Live Scan' choice button. "
                "For all general queries like 'Show me recommendations', 'What are the recommendations', 'Check campaign details', "
                "you MUST set fresh=False (or omit it) to retrieve from storage instantly without API calls. "
                "DO NOT set True just because you want 'current' or 'new' recommendations."
            ),
            required=False,
        ),
        ToolParameter(
            name="question",
            type="string",
            description=(
                "Specific optimization question from the user, e.g. "
                "'Why is my conversion tracking dropping?' or "
                "'Should I switch to Target CPA?'"
            ),
            required=False,
        ),
        ToolParameter(
            name="force_refresh",
            type="boolean",
            description=(
                "Set True ONLY when the user explicitly insists on a new scan after you "
                "already told them a recent analysis exists and explained the cooldown. "
                "Bypasses the cooldown gate and always spawns a live sub-agent scan. "
                "Do NOT set True on the first request — only after the user has acknowledged "
                "the existing analysis and confirmed they want a new one anyway."
            ),
            required=False,
        ),
    ],
    execute=_optimize,
)

OPTIMIZE_TOOLS = [optimize]
