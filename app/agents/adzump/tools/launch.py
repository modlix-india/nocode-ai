"""launch_campaign - persist the assembled campaign to AISuggestedData.

Called by the LLM when the user confirms launch ("Yes, launch"). Writes the
full campaign record (including the analysis snapshot + lat/lng location +
account hierarchy) under the same per-URL key `ds/chatv2` already uses.

This is the single deterministic save action - the LLM's job on launch
confirm is exactly one tool call, no transcription, no field assembly.
Future work: also call `publish_google_campaign` / `publish_meta_campaign`
to actually create the campaign in the ad platform.
"""

from __future__ import annotations

import logging
import re

from app.core.tools.base import ToolDefinition, ToolResult
from app.agents.adzump.platform import to_enum_value as platform_enum_value
from app.agents.adzump.services.business_storage import resolve_url, save_campaign
from app.agents.adzump.tools.campaign_data import _last_user_text, is_clear_decline_reply

logger = logging.getLogger(__name__)

# Word-boundary affirmatives for the consent gate. This is a GATE on an
# irreversible action, not NLU - the model still interprets language and
# decides WHEN to call launch; the harness just refuses when the user's most
# recent message carries no explicit go-ahead (same backstop philosophy as
# _field_traceable / F17: the prompt persuades, the code enforces).
_AFFIRMATIVE_RE = re.compile(
    r"\b(yes|yeah|yep|launch|confirm(?:ed)?|approve(?:d)?|proceed|publish|"
    r"go ahead|do it|sure|ok(?:ay)?)\b"
)


def _user_confirmed_launch(last_user: str) -> bool:
    """True when the user's latest message is an explicit launch go-ahead."""
    lu = (last_user or "").strip().lower()
    if not lu or is_clear_decline_reply(lu):
        return False
    return bool(_AFFIRMATIVE_RE.search(lu))


async def _launch_campaign(params: dict, context: dict) -> ToolResult:
    session_ctx = context.get("session_context")
    if session_ctx is None:
        return ToolResult(success=False, error="No session context available.")

    spec = session_ctx.get("campaign_spec") or {}

    # Idempotency: a double "Yes, launch" click or a model retry must not
    # re-run the save. campaign_status is set on success below; honor it here.
    if spec.get("campaign_status") == "launched" and session_ctx.get("product_id"):
        record_id = session_ctx["product_id"]
        logger.info("launch_campaign_idempotent: already launched, product_id=%s", record_id)
        return ToolResult(
            success=True,
            data={"product_id": record_id},
            summary=(
                f"Campaign was already launched (product reference: {record_id}). "
                "Do not launch again - tell the user it is already live."
            ),
        )

    # Guard: refuse to save a clearly-incomplete spec. Cheap pre-check.
    # (ig_page is intentionally absent - Instagram is optional, v3 · F3.)
    required = ("platform", "duration", "budget", "parent_account", "account")
    missing = [k for k in required if not spec.get(k)]
    if missing:
        return ToolResult(
            success=False,
            error=f"Cannot launch - missing required fields: {', '.join(missing)}.",
        )

    # v3 · F2 defence-in-depth: refuse to persist an account id that belongs to a
    # DIFFERENT ad platform than the one selected - catches a dependency-cascade
    # miss before a stale Google id leaks into a Meta launch (or vice-versa).
    # Only enforced for ids we actually tagged at fetch time (account_platforms);
    # untagged ids from older sessions skip the check (back-compat safe).
    current_platform = platform_enum_value(spec.get("platform"))
    tagged = session_ctx.get("account_platforms") or {}
    mismatched = [
        f for f in ("parent_account", "account", "fb_page", "ig_page")
        if spec.get(f) and tagged.get(str(spec[f])) and tagged[str(spec[f])] != current_platform
    ]
    if mismatched:
        logger.warning("launch_platform_mismatch: platform=%s offending=%s",
                       current_platform, {f: spec.get(f) for f in mismatched})
        return ToolResult(
            success=False,
            error=(
                f"Cannot launch - {', '.join(mismatched)} belong to a different platform "
                f"than {spec.get('platform')}. Re-select them for the current platform."
            ),
        )

    # Consent gate - harness enforcement of the prompt rule "never publish
    # without an explicit yes in the user's most recent message". Runs last,
    # directly before the side effect.
    last_user = _last_user_text(context)
    if not _user_confirmed_launch(last_user):
        logger.warning("launch_blocked_no_consent: last_user=%r", (last_user or "")[:120])
        return ToolResult(
            success=False,
            error=(
                "Cannot launch - the user's most recent message is not an explicit "
                "launch confirmation. Show the review summary and ask with "
                'present_options("Ready to launch?") first; call launch_campaign '
                "only after the user answers yes."
            ),
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
    # so the returned id is the product's stable reference - same id across
    # re-launches for the same URL. Persist it under `product_id` so future
    # turns can resolve it and survive session restore.
    session_ctx["product_id"] = record_id
    spec["campaign_status"] = "launched"
    logger.info("launch_campaign_ok: product_id=%s", record_id)

    # Signal the host page (LazyPrompt onComplete / completeBindingPath) that
    # adzump reached a successful terminal state. Fire-and-forget - failure
    # to emit must not roll back the save.
    stream = context.get("event_stream")
    if stream is not None:
        try:
            await stream.emit_complete({
                "product_id": record_id,
                "session_id": context.get("session_id", ""),
                # Same URL the storage record is keyed by - resolve_url checks
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
        "no parameters - reads everything from session.context. Returns a "
        "campaign id on success."
    ),
    display_name="Launch Campaign",
    parameters=[],
    execute=_launch_campaign,
)


LAUNCH_TOOLS = [launch_campaign]
