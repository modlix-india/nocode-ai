"""Campaign spec tool — stores campaign-defining fields in session context.

The LLM calls ``set_campaign_spec`` to record the parameters that define a
campaign (platform, budget, duration, accounts, etc.) into
``session.context["campaign_spec"]``. Storage is:

- **Idempotent** — re-sending unchanged state is a silent no-op.
- **Value-validated** — free-text fields must trace to the user's most recent
  message; account/page ids must have been returned by a fetch tool.
- **Provenance-tracked** — each field write records the turn it was set in
  (``session.context["_spec_set_at"][field]``), so the dynamic context can
  render "just set" vs "set N turns ago" and the LLM can reason about state
  age.
- **Delta-summarized** — when a field overwrites a prior value, the tool
  summary reads ``"platform (was Google Ads → Meta)"`` so historical assistant
  messages carry the transition.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.platform import Platform
from app.agents.adzump.answer_parse import (
    parse_typed_answer, currency_for, field_candidates,
)

logger = logging.getLogger(__name__)


# Post-NFKC folding collapses fullwidth digits + most Unicode dashes to ASCII.
# Remaining stragglers: soft hyphen (U+00AD) and hyphen bullet (U+2043) don't
# fold. U+2212 (minus) folds but kept explicit for belt-and-suspenders.
_ID_FORMATTING_CHARS = re.compile(r"[\s\-\u00AD\u2043\u2212]")


def _normalize_id(v: Any) -> Any:
    """Canonicalize account/page ids: NFKC-fold, strip dashes + whitespace.

    Preserves alphanumerics and underscore so Meta ``act_984...`` stays intact.
    Coerces non-strings (ints from JSON) to str first. ``None`` passes through.
    """
    if v is None:
        return v
    s = unicodedata.normalize("NFKC", str(v))
    return _ID_FORMATTING_CHARS.sub("", s)


# Fields that can be set via this tool.
ALLOWED_FIELDS = {
    "platform",
    "duration",
    "budget",
    "location",
    "parent_account",
    "account",
    "fb_page",
    "ig_page",
    "competitive_analysis_declined",
    "ig_page_declined",          # v3 · F3 — Instagram is optional; "true" = Facebook-only
}

# IDs from Google Ads / Meta — must be traceable to a fetch tool's output
# (via session_ctx["account_names"]).
_ACCOUNT_LIKE_FIELDS = {"parent_account", "account", "fb_page", "ig_page"}

# Free-text fields whose values must be traceable to the user's most recent
# message. Together with _ACCOUNT_LIKE_FIELDS, every allowed field passes
# through one kind of traceability check. The two decline flags
# (`competitive_analysis_declined`, `ig_page_declined`) have their own narrow
# rules (see _field_traceable).
_USER_TEXT_FIELDS = {"platform", "duration", "budget", "location",
                     "competitive_analysis_declined", "ig_page_declined"}

# v3 · F2 — when a campaign field that OTHERS depend on is *changed*, those
# dependents are now stale and must be cleared. Without this a Google→Meta
# switch leaks the old platform's account ids into the launch payload
# (business_storage builds `accounts` straight from spec), and the forward-only
# `_next_action` never re-asks a field that still looks "set". Keyed by the
# field that changed → the fields it invalidates.
_FIELD_DEPENDENTS: dict[str, tuple[str, ...]] = {
    "platform": ("parent_account", "account", "fb_page", "ig_page",
                 "ig_page_declined", "competitive_analysis_declined"),
    "parent_account": ("account", "fb_page", "ig_page", "ig_page_declined"),
    "fb_page": ("ig_page", "ig_page_declined"),
}

# v3 · F3 — phrases that mean "skip linking Instagram, run Facebook-only".
# Consulted by the ig_page_declined traceability rule (chip-click text like
# "Continue with Facebook only") and by _next_action (typed "skip insta",
# "lets do it later"). Kept narrow + scoped to the IG-pending branch.
_IG_SKIP_PHRASES = (
    "facebook only", "fb only", "without insta", "without instagram",
    "no insta", "no instagram", "skip insta", "skip instagram",
    "continue without", "do it later", "no thanks",
)


def is_ig_skip(text: str) -> bool:
    """True if the user clearly opts out of linking Instagram (Facebook-only)."""
    lu = (text or "").strip().lower()
    if not lu:
        return False
    return any(p in lu for p in _IG_SKIP_PHRASES) or lu in ("skip", "later", "no", "n")


# F11 — phrases that mean "skip competitor analysis". Substring-based, so it's
# comma-robust ("No, skip competitor analysis for now" matches) — unlike the old
# `"no" in lu.split()` which broke on the trailing comma in "no,". Bare no/n/skip
# only as the WHOLE reply, so a polarity-flip ("no, change the budget") is NOT a
# decline. Same shape + role as _IG_SKIP_PHRASES / is_ig_skip.
_DECLINE_PHRASES = (
    "skip competitor", "skip competitors", "skip the competitor", "no competitor",
    "not now", "maybe later", "do it later", "no need", "no thanks",
    "don't bother", "dont bother", "skip it", "skip this", "skip that",
)


def is_decline(text: str) -> bool:
    """True if the user clearly declines the competitor-analysis offer."""
    lu = (text or "").strip().lower()
    if not lu:
        return False
    return any(p in lu for p in _DECLINE_PHRASES) or lu in ("no", "n", "skip", "later")


# F17 — markers that a "decline-looking" reply is actually a request/question for
# something else, so it must NOT be auto-recorded as declined (it goes to the LLM
# instead). is_decline is substring-based and over-fires on these: "not now, first
# tell me about the audience" (defer+ask), "no competitors named yet" (informing).
_DECLINE_AMBIG_MARKERS = (
    "?", "first", "tell me", "what ", "what'", "how ", "which ", "why ",
    "named", "instead", "before we", "about the", "explain", " vs ",
)


def is_clear_decline_reply(text: str) -> bool:
    """True only for a TYPED reply that is an unambiguous decline (no follow-up
    request). Tighter than is_decline — used to auto-record the decline in code
    (deterministic) while leaving doubtful replies for the model to judge. Chip
    clicks are captured separately by exact option match; this covers free text."""
    lu = (text or "").strip().lower()
    if not lu or not is_decline(lu):
        return False
    return not any(m in lu for m in _DECLINE_AMBIG_MARKERS)


def _last_user_text(context: dict[str, Any]) -> str:
    """Most recent user message as a flat string (handles Anthropic list-content)."""
    session = context.get("_session")
    messages = getattr(session, "messages", None) or []
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif isinstance(block, str):
                    parts.append(block)
            return " ".join(parts).strip()
        return str(content).strip()
    return ""


def _current_turn(context: dict[str, Any]) -> int:
    """Monotonic marker for 'this turn'. Uses session._turn_count when available."""
    session = context.get("_session")
    return int(getattr(session, "_turn_count", 0) or 0)


def _field_traceable(field: str, value: Any, last_user: str, session_ctx: dict) -> bool:
    """Can this field's value be traced to what the user actually said?"""
    v = str(value).strip().lower()
    lu = last_user.strip().lower()
    if not v:
        return False

    if field == "location":
        if session_ctx.get("_pending_location_confirm"):
            if lu == "confirm" or "location_update" in lu or lu.startswith("location confirmed"):
                return True
        if lu and v in lu:
            return True
        tokens = [t for t in v.replace(",", " ").split() if len(t) > 3]
        if tokens and sum(1 for t in tokens[:3] if t in lu) >= min(2, len(tokens)):
            return True
        return False

    # Decline flag — accept "true" when the user declines (chip or typed). Uses
    # the shared substring helper (F11: comma-robust; old `"no" in lu.split()`
    # silently rejected "no, skip competitor analysis for now" → re-ask loop).
    if field == "competitive_analysis_declined":
        return v in ("true", "yes", "1") and is_decline(lu)

    # v3 · F3 — Instagram-skip flag. Accept "true" when the user opts out of
    # linking IG, by chip ("Continue with Facebook only") or typed ("skip insta",
    # "do it later"). Same shape as the competitor decline above.
    if field == "ig_page_declined":
        return v in ("true", "yes", "1") and is_ig_skip(lu)

    if lu == v:
        return True
    if v in lu:
        return True
    if field == "platform":
        # Both the LLM-stored value and the user's last message must resolve
        # to the same platform via the shared classifier in `platform.py`.
        # Catches "Google Ads" / "google" / "adwords" → GOOGLE and
        # "Meta" / "facebook" / "instagram" / "fb" / "ig" → META.
        v_platform = Platform.from_value(v)
        if v_platform is not None and Platform.from_value(lu) is v_platform:
            return True
    if field in ("duration", "budget"):
        # PR2 · Option 2 — normalization-aware: a typed reply and a normalized
        # candidate that parse to the SAME canonical value are traceable, even
        # when their raw digits differ ("4k" vs "₹4,000/day"). Hardens the LLM's
        # own save path too.
        #
        # v3 · F1 — the old loose digit-substring fallback (digits_v in
        # digits_lu) was DELETED: it leaked an unrelated number through (a stored
        # "5 days" traced to "I have 15 properties" because "5" ⊂ "15"). The one
        # legitimate case it used to cover — a typed bare number meaning days —
        # is now handled CANONICALLY: parse_typed_answer reads bare "30" → "30
        # days" (duration-only), so both sides parse equal and match above.
        # F24 — read the user's message for ALL values it supports for this field
        # (corrections with a cue word, multi-number volunteered messages), then
        # accept iff the model's canonical value is one of them. The anti-invention
        # property is this canonical equality, NOT a digit-substring — so F1 (a
        # stored "5 days" tracing to "15 properties") stays closed.
        cur = currency_for(session_ctx)
        cand = parse_typed_answer(field, str(value), cur)
        if cand is not None and cand in field_candidates(field, last_user, cur):
            return True
    return False


async def _set_campaign_spec(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Store campaign-spec fields in session context."""
    session_ctx = context.get("session_context")
    if session_ctx is None:
        return ToolResult(success=False, error="No session context available.")

    spec = session_ctx.setdefault("campaign_spec", {})
    set_at = session_ctx.setdefault("_spec_set_at", {})

    # Filter: allowed fields, non-empty, value differs from stored. Normalize
    # account-like fields on both sides so display-form echoes ("446-197-2633"
    # vs stored "4461972633") don't leak through as false changes.
    incoming: dict[str, Any] = {}
    for k, v in params.items():
        if k not in ALLOWED_FIELDS or v in (None, ""):
            continue
        new_v = _normalize_id(v) if k in _ACCOUNT_LIKE_FIELDS else v
        current = spec.get(k)
        current_norm = _normalize_id(current) if k in _ACCOUNT_LIKE_FIELDS else current
        if current_norm == new_v:
            continue
        incoming[k] = new_v

    if not incoming:
        any_supplied = any(params.get(k) for k in ALLOWED_FIELDS)
        if not any_supplied:
            return ToolResult(
                success=False,
                error="No valid fields provided. Allowed: " + ", ".join(sorted(ALLOWED_FIELDS)),
            )
        return ToolResult(success=True, summary="")

    # Validate + write each field through the shared single-field helper, so
    # this tool and PR2 tagged-answer capture can't diverge on the guard. A
    # rejected field doesn't block a valid one in the same call.
    last_user = _last_user_text(context)
    turn = _current_turn(context)
    parts: list[str] = []
    stored_keys: list[str] = []
    kept: list[str] = []
    rejected: list[tuple[str, Any, str]] = []
    # F2 · the whole incoming set is the "batch" — a field changed in this call
    # must never be cleared by another field's cascade in the same call.
    batch_fields = set(incoming.keys())
    for k, v in incoming.items():
        stored, info = _apply_field(k, v, last_user, session_ctx, turn, batch_fields)
        if stored:
            parts.append(info)
            stored_keys.append(k)
        elif k not in _ACCOUNT_LIKE_FIELDS and spec.get(k) not in (None, ""):
            # v5 · an ALREADY-STORED text field re-sent with an unverifiable
            # paraphrase (live loop: stored full address re-sent as
            # "Bengaluru"). Keep the stored value as a no-op SUCCESS — an
            # error here reads as "retry me" and fueled 25+ identical calls.
            # A genuine correction is traceable to the user's message and
            # stores normally above. Account-like fields are EXCLUDED (Kiran):
            # their same-id echoes are already normalized away upstream, so a
            # different unknown id on a stored account field is an attempted
            # switch — it must stay an actionable rejection (re-fetch / hint),
            # never a silent keep.
            kept.append(f"{k} (kept '{spec.get(k)}')")
        else:
            rejected.append((k, v, info))

    if stored_keys or kept:
        session_ctx.pop("_spec_reject_streak", None)

    # If everything was rejected on EMPTY fields, return a guidance error. If
    # anything was stored or kept, keep going — append the rejection note to
    # the summary so the LLM sees which fields it needs to re-ask for.
    if not stored_keys and not kept:
        pairs = ", ".join(f"{k}={v} ({why})" for k, v, why in rejected)
        preview = (last_user or "").replace("\n", " ")[:120]
        logger.warning(
            "campaign_spec_all_rejected: incoming=%s rejected=%s user_said=%r",
            {k: params.get(k) for k in ALLOWED_FIELDS if params.get(k)},
            rejected, preview,
        )
        # v5 · circuit breaker: the same FIELD-SET rejected 3+ times in a row
        # gets a hard stop-steer. F12: key on the field set, NOT (field,value) —
        # a model inventing FRESH values each retry (30d→45d→60d) would otherwise
        # reset the streak every turn and evade the breaker forever. Safe to
        # collapse across values: this path is all-rejected (nothing stored), and
        # any traceable correction stores → pops the streak at :280-281, so a real
        # varied correction never accumulates here.
        sig = repr(sorted(k for k, _, _ in rejected))
        streak = session_ctx.get("_spec_reject_streak") or {}
        n = (streak.get("n", 0) + 1) if streak.get("sig") == sig else 1
        session_ctx["_spec_reject_streak"] = {"sig": sig, "n": n}
        if n >= 3:
            return ToolResult(
                success=False,
                summary="Let me ask you about that instead of guessing.",  # user/card; steer stays model-only (B2b)
                error=(
                    f"STOP — {pairs} rejected {n} times: these values are NOT traceable "
                    "to anything the user said (you may be inventing them). Do NOT call "
                    "set_campaign_spec again — ASK the user via present_options."
                ),
            )
        return ToolResult(
            success=False,
            summary="Let me confirm that with you rather than assume.",  # user/card; steer stays model-only (B2b)
            error=(
                f"Cannot set {pairs} — NOT traceable to the user's last message "
                f"('{preview}'); do not invent or default these. ASK the user via "
                "present_options, then store only what they actually say."
            ),
        )

    # Are we DONE after this write? If yes, inject the review prescription
    # into the tool result so the LLM renders the summary on the same turn —
    # the dynamic context computed at start-of-turn doesn't know the field
    # we just stored is set.
    review_hint = _review_hint_if_complete(spec, session_ctx)

    # v5 · kept no-ops get one steer line so the model stops re-sending them.
    kept_note = (
        f" {'; '.join(kept)} — already set: do NOT re-send stored fields, "
        "only send NEW values the user just gave."
    ) if kept else ""

    if rejected:
        # Partial: log + steer. F17c — if this call stored NOTHING new (only kept
        # paraphrases + rejected invents), it made no forward progress, so flag
        # no_progress and let the core stuck-step breaker count it. A kept+rejected
        # bleed that stores nothing is the same retry-me loop as an all-failed turn
        # (live: 18 consecutive calls; the 'kept' silently disarmed both breakers).
        # When something WAS stored this call it's real progress → no flag.
        # (Supersedes the v5 "don't fire the breaker here" note — that was about the
        # separate reject-streak breaker, not this no_progress/stuck-step path.)
        logger.warning(
            "campaign_spec_partial: stored=%s kept=%s rejected=%s call=%s user_said=%r",
            stored_keys, kept, rejected,
            {k: params.get(k) for k in ALLOWED_FIELDS if params.get(k)},
            (last_user or "").replace("\n", " ")[:120],
        )
        summary_parts = parts + [f"rejected {k}={v} ({why})" for k, v, why in rejected]
        prefix = "Campaign spec updated" if stored_keys else "No changes stored"
        # User/card sees only what was actually stored; the rejection steer + kept/
        # review hints are model-only (B2b: never leak validator internals to chat).
        user_summary = f"Campaign spec updated: {', '.join(parts)}." if stored_keys else "No changes stored."
        return ToolResult(
            success=True,
            summary=user_summary,
            model_summary=f"{prefix}: {', '.join(summary_parts)}.{kept_note}{review_hint}",
            data=None if stored_keys else {"no_progress": True},
        )

    if not stored_keys:
        # kept-only: nothing changed; say so without an error the model would retry.
        # F15: data["no_progress"]=True — this success stored NOTHING new, so the
        # core stuck-loop breaker counts it (a model that ignores the "do NOT
        # re-send" steer and loops kept-noops gets the tool quarantined, same as
        # an all-failed loop). Distinct from the v5 reject-streak (all-rejected).
        logger.info("campaign_spec_noop_kept: kept=%s", kept)
        return ToolResult(
            success=True,
            summary="No changes.",
            model_summary=f"No changes.{kept_note}{review_hint}",
            data={"no_progress": True},
        )

    logger.info("campaign_spec_updated: fields=%s kept=%s", stored_keys, kept)
    clean = f"Campaign spec updated: {', '.join(parts)}."
    return ToolResult(
        success=True,
        summary=clean,
        model_summary=f"{clean}{kept_note}{review_hint}",
    )


def _store_location_meta(session_ctx: dict, location_value: Any, last_user: str) -> None:
    """Location side-effect: clear the map-confirm marker + stash lat/lng for save.
    Plain "confirm" / typed-city paths store the address with null coordinates."""
    session_ctx.pop("_pending_location_confirm", None)
    from app.agents.adzump.services.business_storage import parse_location_update
    loc_payload = parse_location_update(last_user)
    if loc_payload:
        display_name = ""
        product = session_ctx.get("product_data") or {}
        if product.get("product_name") and product.get("location"):
            display_name = f"{product['product_name']}, {product['location']}"
        session_ctx["_location_meta"] = {
            "address": loc_payload["address"] or str(location_value),
            "lat": loc_payload["lat"], "lng": loc_payload["lng"],
            "displayName": display_name,
        }
    else:
        session_ctx["_location_meta"] = {
            "address": str(location_value), "lat": None, "lng": None, "displayName": "",
        }


def _clear_dependents(field: str, session_ctx: dict, batch_fields) -> list[str]:
    """v3 · F2 — clear the fields that depend on ``field`` (now stale because
    ``field`` changed). NEVER clears a field that is being set in the SAME
    set_campaign_spec call (``batch_fields``) — otherwise a bundled
    {platform, account} write would undo its own account. Returns the names
    actually cleared (for the delta string)."""
    deps = _FIELD_DEPENDENTS.get(field)
    if not deps:
        return []
    spec = session_ctx.get("campaign_spec") or {}
    set_at = session_ctx.get("_spec_set_at") or {}
    cleared: list[str] = []
    for dep in deps:
        if dep in batch_fields:
            continue
        if spec.pop(dep, None) is not None:
            set_at.pop(dep, None)
            cleared.append(dep)
    # A changed FB page (or anything upstream of it) also invalidates the
    # "Instagram options were already offered" marker (F3).
    if field in ("platform", "parent_account", "fb_page"):
        session_ctx.pop("_ig_offered", None)
    return cleared


def clear_competitor_decline(session_ctx: dict) -> bool:
    """F26 — competitors are now present (analyzed / looked-up), so a PRIOR
    "declined" is void: a launched/reviewed campaign must never report
    'declined' alongside a populated competitor list (the contradictory state
    reachable via decline→reverse). Pop the flag AND its provenance, mirroring
    _clear_dependents' spec/set_at lockstep. Idempotent. Returns whether it
    popped (for logging)."""
    spec = session_ctx.get("campaign_spec") or {}
    if spec.pop("competitive_analysis_declined", None) is None:
        return False
    (session_ctx.get("_spec_set_at") or {}).pop("competitive_analysis_declined", None)
    return True


def _apply_field(
    field: str, value: Any, last_user: str, session_ctx: dict, turn: int,
    batch_fields=frozenset(),
) -> tuple[bool, str]:
    """Validated single-field write — the one place a campaign_spec field is
    checked and stored. Shared by `set_campaign_spec` (LLM path) and PR2
    `_capture_tagged_answer` (harness path) so they cannot diverge on the
    traceability rule. ``value`` must already be normalized + changed (callers
    filter no-ops). ``batch_fields`` names the other fields being set in the
    same call, so the F2 cascade never clears a freshly-bundled sibling.
    Returns (stored, info): info is the delta string on success or the rejection
    reason on failure."""
    spec = session_ctx.setdefault("campaign_spec", {})
    set_at = session_ctx.setdefault("_spec_set_at", {})
    if field in _USER_TEXT_FIELDS and not _field_traceable(field, value, last_user, session_ctx):
        return (False, "not traceable to user's last message")
    if field in _ACCOUNT_LIKE_FIELDS:
        known_ids = set((session_ctx.get("account_names") or {}).keys())
        if str(value) not in known_ids:
            reason = "not returned by any fetch tool this session"
            if field == "ig_page":
                # v5 · live mangle: the model sent ig_page="true" meaning a
                # Facebook-only decline. Point it at the right key.
                reason += ' — to run Facebook-only, set ig_page_declined="true" instead'
            return (False, reason)
    prior = spec.get(field)
    spec[field] = value
    set_at[field] = turn
    if field == "location":
        _store_location_meta(session_ctx, value, last_user)
    # v3 · F2 — cascade ONLY on a genuine change (overwrite), never on first-set
    # or an idempotent re-send (prior empty / equal → no clear). This is what
    # makes a Google→Meta switch drop the stale Google ids while a re-fire of
    # the same value is a safe no-op.
    if prior not in (None, "") and str(prior) != str(value):
        cleared = _clear_dependents(field, session_ctx, batch_fields)
        info = f"{field} (was {prior} → {value})"
        if cleared:
            info += f"; cleared stale {', '.join(cleared)}"
        return (True, info)
    return (True, field)


def _review_hint_if_complete(spec: dict, session_ctx: dict) -> str:
    """If every required campaign-spec field is now set, return a string that
    instructs the LLM to render the review summary on this same turn.
    Otherwise return ''."""
    platform = Platform.from_value(spec.get("platform"))
    if platform is None:
        return ""
    is_google = platform is Platform.GOOGLE
    is_meta = platform is Platform.META

    # Real-estate? location is required. Otherwise location is optional.
    business_type = ((session_ctx.get("product_data") or {})
                     .get("business_type") or "").lower()
    is_real_estate = any(kw in business_type for kw in (
        "real estate", "realty", "villa", "apartment", "residential",
        "property", "housing", "homes", "realtor", "township",
        "builder", "developer",
    ))
    if is_real_estate and not spec.get("location"):
        return ""

    if not (spec.get("duration") and spec.get("budget")
            and spec.get("parent_account") and spec.get("account")):
        return ""

    # Google: competitive analysis must have been attempted OR declined.
    if is_google:
        if (session_ctx.get("competitor_analysis") is None
                and "competitive_analysis_declined" not in spec):
            return ""

    # Meta: fb_page required; Instagram is OPTIONAL (v3 · F3) — but it must have
    # been OFFERED, i.e. an ig_page was picked OR ig_page_declined is set. This
    # gates review until the IG choice has been made once, without making IG
    # mandatory (Facebook-only is a valid campaign).
    if is_meta and not (spec.get("fb_page")
                        and (spec.get("ig_page") or spec.get("ig_page_declined"))):
        return ""

    meta_extra = ""
    if is_meta:
        meta_extra = "\n  - **Facebook Page**: <copy verbatim from State, including '(ID: …)'>"
        meta_extra += (
            "\n  - **Instagram Account**: <copy verbatim from State, including '(ID: …)'>"
            if spec.get("ig_page")
            else "\n  - **Instagram Account**: not linked (Facebook only)"
        )
    return (
        "\n\nALL CAMPAIGN FIELDS ARE NOW SET. Do NOT call any other tool yet. "
        "Render this exact markdown summary on this turn — copy values VERBATIM "
        "from the `## State` block in the system prompt (do not rephrase, do "
        "not drop fields, do not replace IDs with placeholders like 'Linked' "
        "or 'Connected'):\n\n"
        "Here's your campaign summary:\n\n"
        "  - **Product**: <product name from State>\n"
        "  - **Website**: <website URL from State>\n"
        "  - **Location**: <location from State>\n"
        "  - **Platform**: <platform from State>\n"
        "  - **Duration**: <duration from State>\n"
        "  - **Daily Budget**: <budget from State>\n"
        "  - **Manager / Business Account**: <copy verbatim from State, including '(ID: …)'>\n"
        "  - **Ad Account**: <copy verbatim from State, including '(ID: …)'>"
        f"{meta_extra}\n"
        "  - **Competitors**: <comma-separated names from State, or 'none "
        "analyzed', or 'declined' if competitive_analysis_declined='true'>\n\n"
        "Then call `present_options(question=\"Ready to launch the campaign?\", "
        "options=[\"Yes, launch\", \"No, make changes\"])`. EVERY bullet must "
        "be present. **On the user's 'Yes, launch' reply, call "
        "`launch_campaign()` (no params) — that's the one tool that persists "
        "the campaign.**"
    )


set_campaign_spec = ToolDefinition(
    name="set_campaign_spec",
    description=(
        "Store campaign-spec fields — the parameters that define the campaign "
        "being built (platform, budget, duration, location, account selection). "
        "Call this whenever the user provides a value via button click, typed "
        "text, or a widget callback. You can set multiple fields in a single "
        "call when the user provides them together in one message."
    ),
    display_name="Update Campaign Spec",
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
            description="Daily budget (e.g., '₹5,000/day')",
            required=False,
        ),
        ToolParameter(
            name="location",
            type="string",
            description="Confirmed project location for real-estate campaigns (city/area).",
            required=False,
        ),
        ToolParameter(
            name="parent_account",
            type="string",
            description=(
                "Selected parent account id — Google Ads manager (MCC) customer_id "
                "or Meta Business id, depending on platform."
            ),
            required=False,
        ),
        ToolParameter(
            name="account",
            type="string",
            description=(
                "Selected final ad account id — child of parent_account. "
                "Google Ads customer_id or Meta ad account id."
            ),
            required=False,
        ),
        ToolParameter(
            name="fb_page",
            type="string",
            description="Meta only — Facebook page id the campaign will post from.",
            required=False,
        ),
        ToolParameter(
            name="ig_page",
            type="string",
            description="Meta only — Instagram Business account id linked to the fb_page.",
            required=False,
        ),
    ],
    execute=_set_campaign_spec,
)

CAMPAIGN_SPEC_TOOLS = [set_campaign_spec]
