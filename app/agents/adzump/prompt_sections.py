"""Per-turn prompt-section renderers for the adzump orchestrator.

Pure text builders: each renders one ``##`` section of the turn reminder
(State / User just said / What's still missing / How to respond) from the
typed ``CampaignContext`` or plain values. No I/O, no session access -
split out of agent.py alongside workflow.py.
"""

from __future__ import annotations

from app.agents.adzump.models import OfferState, offer_state
from app.agents.adzump.workflow import CampaignContext
from app.agents.adzump.platform import Platform


def _state_section(cctx: CampaignContext) -> str:
    lines = ["## State"]

    if cctx.product:
        parts: list[str] = []
        if name := cctx.product.get("product_name"):
            parts.append(name)
        if bt := cctx.product.get("business_type"):
            parts.append(f"({bt})")
        lines.append(f"- Product: {' '.join(parts) or '(unnamed)'}")
    else:
        lines.append("- Product: - (need URL)")

    url = website_display(cctx)
    if url != "-":
        lines.append(f"- Website: {url}")

    if cctx.competitor_names:
        names = ", ".join(cctx.competitor_names[:5])
        suffix = (
            f" (+{len(cctx.competitor_names) - 5} more)"
            if len(cctx.competitor_names) > 5
            else ""
        )
        lines.append(f"- Competitors: {names}{suffix} ✓")
    elif (
        cctx.competitor_analysis_attempted
        or offer_state(cctx.spec, "competitive_analysis") is OfferState.DECLINED
    ):
        lines.append("- Competitors: none analyzed")

    for key, label in (
        ("location", "Location"),
        ("platform", "Platform"),
        ("duration", "Duration"),
        ("budget", "Budget"),
    ):
        val = cctx.spec.get(key)
        prov = _provenance(key, cctx.set_at, cctx.current_turn)
        if val:
            lines.append(f"- {label}: {val} ✓{prov}")
        else:
            lines.append(f"- {label}: -")

    target_areas = cctx.product.get("target_areas") or []
    if target_areas:
        area_names = [a.get("name") for a in target_areas if a.get("name")]
        lines.append(f"- Target Areas: {', '.join(area_names)} ✓")

    account_block = _ad_account_summary(cctx.spec, cctx.account_names)
    if account_block.strip():
        lines.append(account_block.rstrip())

    return "\n".join(lines)


def _provenance(field_name: str, set_at: dict, current_turn: int) -> str:
    if field_name not in set_at:
        return ""
    turn = int(set_at[field_name])
    delta = max(0, current_turn - turn)
    if delta == 0:
        return " - just set"
    if delta == 1:
        return " - set 1 turn ago"
    return f" - set {delta} turns ago"

def _user_said_section(last_user: str) -> str:
    if not last_user:
        return "\n## User just said\n(no user message yet)"
    # Keep the user's line structure - flattening "1. X\n2. Y\n3. Z" into one
    # line turned a typed competitor LIST into a paragraph the model half-read
    # (live 2026-09-09). Fenced so the model sees it verbatim.
    preview = last_user.strip()
    if len(preview) > 500:
        preview = preview[:500] + "…"
    return f"\n## User just said\n'''\n{preview}\n'''"

def _missing_section(missing: list[str]) -> str:
    if not missing:
        # Reachable exactly when a step is WAITING (its ask is on screen and
        # blocks completion) - never claim review-readiness here or the
        # review-over-open-ask behavior the ready gate kills leaks back in.
        return ("\n## What's still missing\n(nothing to ask - an answer is "
                "pending on screen; wait for the user's reply)")
    # Render each pending item with its full prescription. Top-1 is
    # marked as the immediate next action; the rest let the LLM keep
    # going within the same agentic-loop turn (e.g. after storing
    # platform, call confirm_location for location).
    lines = [
        "\n## What's still missing (in order - do the top item first)",
        "Example values below (e.g. \"30 days\", \"₹5,000/day\") are OPTIONS to "
        "SHOW the user via present_options - NEVER values to store. Only "
        "`set_campaign_spec` a field after the user actually states it (F12).",
    ]
    for i, item in enumerate(missing, 1):
        lines.append(f"{i}. {item}")
    return "\n".join(lines)

def _how_to_respond_section() -> str:
    return (
        "\n## How to respond (first match wins — Rule 1 OVERRIDES everything below, including Next action)\n"
        "1. Targeting-location edit → call `manage_targeting_locations(user_message=<their verbatim "
        "message>)` IMMEDIATELY, BEFORE any other step, even if Next action says "
        "\"EXACTLY this\" or has multi-step instructions. After it completes, "
        "re-check Next action.\n"
        "   Triggers (any of these): structured widget messages (\"add targeting location …\", "
        "\"delete targeting location …\"); natural-language requests to add a place "
        "(\"add Koramangala\", \"include HSR Layout\", \"target Whitefield too\"); "
        "natural-language requests to remove a place "
        "(\"remove Indiranagar\", \"delete Koramangala\", \"take out Whitefield\", "
        "\"don't include that area\", \"remove the last one\"); "
        "requests to clear or replace the whole list (\"clear all locations\", "
        "\"change targeting to just Bangalore\").\n"
        "2. Info question → answer briefly from State, then do the Next action.\n"
        "3. Correction → `set_campaign_spec(<field>=<new>)`, acknowledge, then re-check Next action.\n"
        "4. **New data** (typed or chip-clicked) → `set_campaign_spec(<field>=<value>)` IMMEDIATELY, "
        "even if the value is for a different field than Next action. "
        'Examples: user says "Google Ads" → `set_campaign_spec(platform="Google Ads")`. '
        'User says "₹10,000/day" → `set_campaign_spec(budget="₹10,000/day")`. '
        "Then acknowledge in one short sentence and re-check Next action.\n"
        '5. Ambient ("ok", "continue", "next") → just do Next action.\n'
        "6. Otherwise → do Next action.\n"
        "\n**A tool already spoke?** When a tool posts its own result to the "
        "user (assets saved/skipped/corrected, competitors added/skipped - these "
        "now appear in chat automatically), do NOT repeat it; write only a short "
        "one-line lead-in to the Next action.\n"
        "\n**One ask per turn.** Never call two question-asking tools "
        "(`confirm_location`, `present_options`) in the same turn - ask one, "
        "wait for the reply, then ask the next. (The runtime also enforces "
        "this, but don't rely on it.)\n"
        "\n**Structure your chat text.** Whenever you list items "
        "(competitors, options recap, what changed), write markdown bullets "
        "with a blank line before the list - never a comma-run paragraph.\n"
        "\n**Tool syntax is INTERNAL - never print it.** The `tool(question=…, "
        "options=[…], field=…)` forms in '## What's still missing' are "
        "instructions for YOU to CALL - never text to show the user. CALL the "
        "tool; your visible reply is natural prose only. NEVER write a tool "
        "name or `tool(...)` call syntax into the chat."
    )

def website_display(cctx: CampaignContext) -> str:
    """The analyzed business URL - the ONE fallback chain (profile url ->
    first analyzed page -> '-') shared by the State block and the review card
    so the two renderings can never drift."""
    return (
        cctx.product_profile.get("url")
        or (cctx.product.get("pages_analyzed") or [None])[0]
        or "-"
    )


def account_display(
    acct_id: str | None, account_names: dict, platform_value: str | None,
) -> str:
    """'{Name} (ID: {id})' for an account-like spec field - the ONE format
    both the State block and the review card use, so an ID can never degrade
    to a placeholder like 'Linked'. Google CIDs render dashed."""
    if not acct_id:
        return "-"
    raw = str(acct_id)
    display_id = raw
    if Platform.from_value(platform_value) is Platform.GOOGLE \
            and raw.isdigit() and len(raw) == 10:
        display_id = f"{raw[:3]}-{raw[3:6]}-{raw[6:]}"
    name = (account_names.get(raw) or "").strip()
    return f"{name} (ID: {display_id})" if name else f"ID: {display_id}"


def _ad_account_summary(spec: dict, account_names: dict) -> str:
    platform = Platform.from_value(spec.get("platform"))
    if platform is None:
        return ""
    is_meta_platform = platform is Platform.META
    is_google_platform = platform is Platform.GOOGLE
    parent_label = (
        "Meta Business"
        if is_meta_platform
        else "Google Manager"
        if is_google_platform
        else "Parent Account"
    )
    account_label = (
        "Meta Ad Account"
        if is_meta_platform
        else "Google Ad Account"
        if is_google_platform
        else "Ad Account"
    )

    def fmt(acct_id: str | None) -> str:
        return account_display(acct_id, account_names, spec.get("platform"))

    lines = [
        f"- {parent_label}: {fmt(spec.get('parent_account'))}",
        f"- {account_label}: {fmt(spec.get('account'))}",
    ]
    if is_meta_platform:
        lines.append(f"- Facebook Page: {fmt(spec.get('fb_page'))}")
        lines.append(f"- Instagram Account: {fmt(spec.get('ig_page'))}")
    return "\n".join(lines)
