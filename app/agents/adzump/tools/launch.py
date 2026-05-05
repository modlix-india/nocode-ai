"""launch_campaign — persist the assembled campaign to AISuggestedData.

Called by the LLM when the user confirms launch ("Yes, launch"). Writes the
full campaign record (including the analysis snapshot + lat/lng location +
account hierarchy) under the same per-URL key `ds/chatv2` already uses.

This is the single deterministic save action — the LLM's job on launch
confirm is exactly one tool call, no transcription, no field assembly.
Future work: also call `publish_google_campaign` / `publish_meta_campaign`
to actually create the campaign in the ad platform.
"""

from __future__ import annotations

import logging

from app.core.tools.base import ToolDefinition, ToolResult
from app.agents.adzump.platform import to_enum_value as platform_enum_value
from app.agents.adzump.services.business_storage import resolve_url, save_campaign

logger = logging.getLogger(__name__)


async def _launch_campaign(params: dict, context: dict) -> ToolResult:
    session_ctx = context.get("session_context")
    if session_ctx is None:
        return ToolResult(success=False, error="No session context available.")

    spec = session_ctx.get("campaign_spec") or {}
    # Guard: refuse to save a clearly-incomplete spec. Cheap pre-check.
    required = ("platform", "duration", "budget", "parent_account", "account")
    missing = [k for k in required if not spec.get(k)]
    if missing:
        return ToolResult(
            success=False,
            error=f"Cannot launch — missing required fields: {', '.join(missing)}.",
        )

    record_id = await save_campaign(session_ctx, context)
    if not record_id:
        return ToolResult(
            success=False,
            error=(
                "Storage save failed. The campaign was NOT saved. "
                "Tell the user the launch couldn't be recorded and to retry."
            ),
        )

    # The storage record is keyed by businessUrl (one record per product),
    # so the returned id is the product's stable reference — same id across
    # re-launches for the same URL. Persist it under `product_id` so future
    # turns can resolve it and survive session restore.
    session_ctx["product_id"] = record_id
    spec["campaign_status"] = "launched"
    logger.info("launch_campaign_ok: product_id=%s", record_id)

    # Signal the host page (LazyPrompt onComplete / completeBindingPath) that
    # adzump reached a successful terminal state. Fire-and-forget — failure
    # to emit must not roll back the save.
    stream = context.get("event_stream")
    if stream is not None:
        try:
            await stream.emit_complete({
                "product_id": record_id,
                "session_id": context.get("session_id", ""),
                # Same URL the storage record is keyed by — resolve_url checks
                # product_profile.url, then product_data.pages_analyzed[0],
                # so it works after a fresh scrape AND after a storage hydrate
                # (where product_data.primary_url is not preserved).
                "product_url": resolve_url(session_ctx),
                # Which ad platform the user picked, normalized to a stable
                # enum ("google" / "meta" / "") so host pages can branch on
                # it without parsing free-text variants like "Google Ads".
                "platform": platform_enum_value(spec.get("platform")),
            })
        except Exception as e:
            logger.warning("emit_complete_failed: %s", str(e)[:200])

    return ToolResult(
        success=True,
        data={"product_id": record_id},
        summary=(
            f"Campaign launched successfully. Product reference: {record_id}. "
            f"Tell the user the campaign is launched and share the product reference."
        ),
    )


launch_campaign = ToolDefinition(
    name="launch_campaign",
    description=(
        "Persist the user's assembled campaign to storage. Call this exactly "
        "once when the user clicks 'Yes, launch' on the review chip. Takes "
        "no parameters — reads everything from session.context. Returns a "
        "campaign id on success."
    ),
    display_name="Launch Campaign",
    parameters=[],
    execute=_launch_campaign,
)


LAUNCH_TOOLS = [launch_campaign]
