"""launch_campaign - create the campaign on the ad platform, then record it.

Called by the LLM when the user confirms launch ("Yes, launch"). The LLM's job is exactly one
tool call, no transcription, no field assembly.

Order is publish then save, so the record never describes a launch that did not happen - and
so a failed publish leaves campaign_status untouched and simply retries. A channel with no
emitter yet (Search) skips the publish and keeps the pre-posting behaviour.

What the record holds is the per-URL business record `ds/chatv2` shares. The created
campaign's resource name is NOT persisted - it is logged only, so a launched campaign is
traceable from the logs but cannot be resolved from the record.
"""

from __future__ import annotations

import logging
import re

from app.agents.adzump.agents.campaign.google.publish import publish_campaign
from app.agents.adzump.agents.campaign.models import (
    build_gaps,
    is_build_complete,
    resolve_channel,
)
from app.agents.adzump.platform import is_google as platform_is_google
from app.agents.adzump.platform import to_enum_value as platform_enum_value
from app.agents.adzump.services.business_storage import resolve_url, save_campaign
from app.agents.adzump.tools.campaign_data import (
    _last_user_text,
    is_clear_decline_reply,
)
from app.core.tools.base import ToolDefinition, ToolResult

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

# Stored when a campaign exists but Google did not hand back its resource name - the
# idempotency guard tests truthiness, and "" would let a retry create a duplicate.
_UNCONFIRMED = "created (resource name unconfirmed)"
# Distinct from _UNCONFIRMED: there the campaign EXISTS and only its name is missing; here
# nobody knows whether it exists. Both bar a retry, only one is a launch.
_UNCERTAIN = "launch timed out (existence unknown)"


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
    if spec.get("campaign_status") == "launched" and (
        session_ctx.get("product_id") or session_ctx.get("launched_campaign")
    ):
        if session_ctx.get("launched_campaign") == _UNCERTAIN:
            # The publish timed out. Retrying is still barred - Google may hold the campaign
            # - but nothing here knows that it does, so this must not read as a success.
            logger.warning("launch_campaign_idempotent: previous publish was uncertain")
            return ToolResult(
                success=False,
                error=(
                    "The earlier launch timed out and it is not known whether Google created "
                    "the campaign. Do NOT launch again - that could create a second one. Tell "
                    "the user to check the Google Ads account."
                ),
            )
        record_id = session_ctx.get("product_id") or session_ctx["launched_campaign"]
        logger.info(
            "launch_campaign_idempotent: already launched, product_id=%s", record_id
        )
        return ToolResult(
            success=True,
            data={"product_id": record_id},
            summary=(
                f"Campaign was already launched (product reference: {record_id}). Do not "
                "launch again - tell the user it exists and is PAUSED, not yet serving."
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

    # The consent gate below only reads an affirmative, so any "yes" can reach here. Meta has
    # no build stage; a Google campaign with no keywords or audience is not launchable.
    if platform_is_google(spec.get("platform")) and not is_build_complete(session_ctx):
        gaps = build_gaps(session_ctx)
        logger.warning(
            "launch_blocked_no_build: spec=%s gaps=%d",
            spec.get("channel") or "-",
            len(gaps),
        )
        # A build with gaps is built - it just owes work, and each gap names the tool that
        # settles it. Sending those to prepare_campaign_review rebuilds what is already there.
        return ToolResult(
            success=False,
            error=(
                "Cannot launch - " + gaps[0]
                if gaps
                else (
                    "Cannot launch - this campaign has not been built yet. Call "
                    "prepare_campaign_review first and let the user review the panel."
                )
            ),
        )

    # v3 · F2 defence-in-depth: refuse to persist an account id that belongs to a
    # DIFFERENT ad platform than the one selected - catches a dependency-cascade
    # miss before a stale Google id leaks into a Meta launch (or vice-versa).
    # Only enforced for ids we actually tagged at fetch time (account_platforms);
    # untagged ids from older sessions skip the check (back-compat safe).
    current_platform = platform_enum_value(spec.get("platform"))
    tagged = session_ctx.get("account_platforms") or {}
    mismatched = [
        f
        for f in ("parent_account", "account", "fb_page", "ig_page")
        if spec.get(f)
        and tagged.get(str(spec[f]))
        and tagged[str(spec[f])] != current_platform
    ]
    if mismatched:
        logger.warning(
            "launch_platform_mismatch: platform=%s offending=%s",
            current_platform,
            {f: spec.get(f) for f in mismatched},
        )
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
        logger.warning(
            "launch_blocked_no_consent: last_user=%r", (last_user or "")[:120]
        )
        return ToolResult(
            success=False,
            error=(
                "Cannot launch - the user's most recent message is not an explicit "
                "launch confirmation. Show the review summary and ask with "
                'present_options("Ready to launch?") first; call launch_campaign '
                "only after the user answers yes."
            ),
        )

    # Create the campaign on the platform BEFORE saving: the record must never describe a
    # launch that did not happen. A channel with no emitter yet (Search) falls through to the
    # pre-posting behaviour rather than being treated as a failed launch.
    outcome = await publish_campaign(session_ctx, context)
    if outcome.uncertain:
        # Google may already hold the campaign. Marking it launched blocks the retry that
        # would create a second one; the user is told to check rather than reassured.
        spec["campaign_status"] = "launched"
        session_ctx["launched_campaign"] = _UNCERTAIN
        logger.error("launch_publish_uncertain: %s", outcome.message)
        return ToolResult(success=False, error=outcome.message)
    if outcome.supported and not outcome.ok:
        # Nothing saved and campaign_status untouched, so the idempotency guard above does
        # not trip and the user can simply retry.
        logger.warning("launch_publish_failed: %s", outcome.message)
        return ToolResult(success=False, error=outcome.message)
    if outcome.dry_run:
        # Nothing was created, so there is no launch to record. Saying so plainly beats a
        # success message the user would read as "we are live".
        return ToolResult(
            success=True,
            summary=(
                f"{outcome.message} Tell the user the campaign was checked against Google "
                "but NOT created, because publishing is in dry-run mode."
            ),
            data={"dry_run": True},
        )

    # Only true when a campaign really exists at Google. An unsupported channel published
    # NOTHING, so everything below has to stop claiming it did.
    published = outcome.supported
    if published:
        # Marked BEFORE the save: the campaign exists, so a failed save must still block a
        # retry. A resource name Google did not return still has to be truthy for the guard.
        spec["campaign_status"] = "launched"
        session_ctx["launched_campaign"] = outcome.campaign or _UNCONFIRMED

    record_id = await save_campaign(session_ctx, context)
    if not record_id:
        if published:
            # The campaign EXISTS at this point, so "retry" would create a second one.
            created = outcome.campaign or "(resource name not returned)"
            logger.error("launch_saved_failed_after_publish: campaign=%s", created)
            return ToolResult(
                success=False,
                error=(
                    f"The campaign WAS created on the ad platform ({created}) but saving the "
                    "record failed. Tell the user it exists and is paused, give them that "
                    "reference, and do NOT launch again - a retry would create a duplicate."
                ),
            )
        logger.warning("launch_save_failed_nothing_published")
        return ToolResult(
            success=False,
            error=(
                "Could not save the campaign record. Nothing was created on the ad platform, "
                "so it is safe to try again."
            ),
        )

    # The save IS the launch for a channel with no emitter yet, so mark it only once it stuck.
    spec["campaign_status"] = "launched"
    channel = (
        resolve_channel(spec).google_channel_type.value
        if platform_is_google(spec.get("platform"))
        else ""
    )

    # The storage record is keyed by businessUrl (one record per product),
    # so the returned id is the product's stable reference - same id across
    # re-launches for the same URL. Persist it under `product_id` so future
    # turns can resolve it and survive session restore.
    session_ctx["product_id"] = record_id
    # The campaign resource name is not persisted - logged so it is at least traceable.
    logger.info(
        "launch_campaign_ok: product_id=%s campaign=%s",
        record_id,
        outcome.campaign or "-",
    )

    # Signal the host page (LazyPrompt onComplete / completeBindingPath) that
    # adzump reached a successful terminal state. Fire-and-forget - failure
    # to emit must not roll back the save.
    stream = context.get("event_stream")
    if stream is not None:
        try:
            await stream.emit_complete(
                {
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
                    "channel": channel,  # "SEARCH" / "DEMAND_GEN", empty for Meta
                    # Redirect to Adzump only when false - its bridge would create a second.
                    "published": published,
                }
            )
        except Exception as e:
            logger.warning("emit_complete_failed: %s", str(e)[:200])

    return ToolResult(
        success=True,
        data={"product_id": record_id},
        summary=(
            (
                f"Campaign created on Google and left PAUSED ({outcome.campaign}). Tell the "
                "user it now exists in their ad account, that it is paused until they turn "
                f"it on, and share the reference {record_id}."
            )
            if published
            else (
                f"Campaign details saved (reference: {record_id}). Tell the user their setup "
                "is saved and they are being taken to the suggestions page to finish "
                "launching it. Do NOT say the campaign is live."
            )
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
