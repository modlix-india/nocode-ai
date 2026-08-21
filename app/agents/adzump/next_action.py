"""The adzump orchestrator's workflow tree - pure decision logic.

``CampaignContext`` is a typed, frozen read-model over ``session.context``;
``_next_action`` computes the ordered missing-list (with the exact tool call
to make per item) from it. All pure functions - no I/O, no session mutation -
split out of agent.py so the most test-valuable code in the orchestrator
lives in a leaf module.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from app.core.session import BaseSession
from app.agents.adzump.models import LEGACY_DECLINED_KEYS, OfferState, offer_state
from app.agents.adzump.platform import (
    is_google as _platform_is_google,
    is_mapped_for,
    is_meta as _platform_is_meta,
)
from app.agents.adzump.tools.campaign_data import (
    _last_user_text,
    competitor_creatives_offer_resolved,
    is_ig_skip,
    is_real_estate,
    wants_competitor_creatives,
)


# R12 · a required slot asked this many times without landing switches to the
# "help me pick" escape - the user may be unsure; never a silent default (F17).
ESCAPE_AFTER_ASKS = 3


@dataclass(frozen=True)
class CampaignContext:
    """Typed read-model over ``session.context``.

    Shields ``_next_action`` and the renderers from raw-dict shape drift.
    Construct per turn via ``from_session``; never mutated after construction.
    """

    product: dict
    product_profile: dict
    competitor_names: list[str]
    competitor_analysis_attempted: bool
    spec: dict
    account_names: dict
    set_at: dict[str, int]
    current_turn: int
    last_user: str
    # Detected location string when `confirm_location` has shown the map and
    # we're awaiting the user's reply. None when no map is in flight.
    pending_location: str | None
    # v3 · F3 - True once fetch_meta_ig_accounts stored its result (the
    # ``ig_accounts`` data key, [] when none are linked). Stops _next_action
    # from re-prescribing the IG fetch every turn - data-backed; the old
    # offered marker is deleted (slice 1d).
    ig_accounts_fetched: bool = False
    # The open elicitation's field, if any - the CURRENT ask on screen. An
    # offer whose rail is open is waiting on the reply, never re-prescribed.
    pending_ask_field: str | None = None
    # Times each field-tagged ask has been shown (present_options counter).
    # Drives offer exhaustion and the refused-required-slot escape (R12).
    field_asks: dict[str, int] = dc_field(default_factory=dict)
    # True once the Meta creative-inspiration offer is settled (fetched,
    # declined, or moot) - computed by the shared predicate in campaign_data so
    # this gate and the review gate can never disagree. Stops the offer from
    # re-firing. Defaulted for direct test construction.
    competitor_creatives_offer_resolved: bool = False

    @classmethod
    def from_session(cls, session: BaseSession) -> "CampaignContext":
        ctx = session.context
        competitive_raw = ctx.get("competitor_analysis")
        competitive = competitive_raw or {}
        # Sole writer (tools/location.py) stores the detected location string.
        pending_location = ctx.get("_pending_location_confirm") or None
        pe = ctx.get("_pending_elicitation") or {}
        # The current ask's field, canonicalized so a legacy in-flight rail
        # (an old *_declined field name) matches the enum offer field.
        pending_ask_field = pe.get("field")
        for offer_field, legacy in LEGACY_DECLINED_KEYS.items():
            if pending_ask_field == legacy:
                pending_ask_field = offer_field
        return cls(
            product=ctx.get("product_data") or {},
            product_profile=ctx.get("product_profile") or {},
            competitor_names=[
                c.get("name")
                for c in (competitive.get("competitors") or [])
                if c.get("name")
            ],
            # True iff `analyze_competitors` ran this session - even if it
            # found 0 verified competitors. This drops the "ask the question"
            # line from missing once the question's been answered.
            competitor_analysis_attempted=competitive_raw is not None,
            spec=ctx.get("campaign_spec") or {},
            account_names=ctx.get("account_names") or {},
            set_at=ctx.get("_spec_set_at") or {},
            current_turn=int(getattr(session, "_turn_count", 0) or 0),
            last_user=_last_user_text({"_session": session}),
            pending_location=pending_location,
            ig_accounts_fetched=ctx.get("ig_accounts") is not None,
            pending_ask_field=pending_ask_field,
            field_asks=dict(ctx.get("_field_asks") or {}),
            competitor_creatives_offer_resolved=competitor_creatives_offer_resolved(
                ctx.get("campaign_spec") or {}, ctx
            ),
        )

    @property
    def is_real_estate(self) -> bool:
        return is_real_estate(self.product.get("business_type") or "")

    @property
    def is_google(self) -> bool:
        return _platform_is_google(self.spec.get("platform"))

    @property
    def is_meta(self) -> bool:
        return _platform_is_meta(self.spec.get("platform"))

    @property
    def has_mapped_geo_targets(self) -> bool:
        return is_mapped_for(
            self.product.get("target_areas"), self.spec.get("platform")
        )


def _next_action(cctx: CampaignContext) -> list[str]:
    """Compute the ordered list of what's still missing, with concrete tool calls.

    Pure function over ``CampaignContext``. Each line names the exact tool
    call to make - including a suggested ``question`` argument for chip
    questions - so the LLM has nothing to construct, only to copy.
    """
    missing: list[str] = []

    if not cctx.product:
        missing.append("business URL - call `analyze_product(url=<the user's URL>)`")
        return missing

    # (The old _detect_intent platform special-case is retired, slice 1b: a
    # platform chip click is captured at layer 1 via the tagged answer_map; a
    # typed cross-field answer lands via the steered model + validation.)

    if cctx.is_real_estate and not cctx.spec.get("location"):
        if cctx.pending_location:
            # Map shown last turn. Branch on user reply.
            detected = cctx.pending_location
            missing.append(
                f"location - map shown for **'{detected}'**. "
                f'If user said `"confirm"` → `set_campaign_spec(location="{detected}")`. '
                f'If JSON `{{"type":"location_update","address":"X",...}}` → '
                f'`set_campaign_spec(location="X")` (use address; fall back to '
                f'`"{detected}"`). '
                f"If user said WRONG/INCORRECT/NOT RIGHT → call `confirm_location()` again. "
                f"If user named a DIFFERENT city → `set_campaign_spec(location=<what they said>)`."
            )
        else:
            missing.append(
                "location - call `confirm_location()` (real estate business)"
            )

    if not cctx.spec.get("platform"):
        missing.append(
            "platform - use the present_options tool (field \"platform\") to ask "
            "\"Which platform should we run this on?\" with chips Google Ads / "
            "Meta, each carrying answer == its value. CALL the tool - never "
            "type the call into your reply."
        )

    has_platform = bool(cctx.spec.get("platform"))
    has_location = bool(cctx.spec.get("location"))
    # Coordinates restored from storage count as a valid anchor for discovery -
    # manage_targeting_locations falls back to product_data.place lat/lng when no
    # location string is provided, so we can prescribe it without a fresh confirm.
    _place = cctx.product.get("place") or {}
    has_geo_anchor = has_location or _place.get("lat") is not None
    if has_platform and has_geo_anchor and not cctx.has_mapped_geo_targets:
        loc_arg = cctx.spec.get("location") or ""
        missing.append(
            (
            'target_areas - call `manage_targeting_locations(user_message="set up geo targeting")`'
            if not loc_arg
            else f'target_areas - call `manage_targeting_locations(user_message="set up geo targeting for {loc_arg!r}")`'
        )
        )

    analysis_state = offer_state(cctx.spec, "competitive_analysis")
    if (
        cctx.is_google
        and not cctx.competitor_analysis_attempted
        and analysis_state is not OfferState.DECLINED
    ):
        # F11 · agentic, not a hardcoded phrase ladder: the MODEL interprets the
        # user's reply to THIS competitor offer (the old `lu in (...)` exact-match
        # missed "No, skip competitor analysis for now" → re-ask loop). Scoped +
        # biased to re-ask on doubt so a polarity-flip ("no, change the budget")
        # is never read as a decline. The _field_traceable guard backstops it.
        if analysis_state is OfferState.ACCEPTED:
            # Every chip writes: the Yes already landed as ACCEPTED - the ask
            # is settled, only the analysis itself is owed.
            missing.append(
                "competitive analysis - the user already said YES to it. Run "
                "`analyze_competitors` NOW; do NOT re-ask. (This is an "
                "instruction to CALL the tool - never type tool-call syntax "
                "into your reply.)"
            )
        else:
            missing.append(
                "competitive analysis - offer it ONCE as a Yes/No question, then react:\n"
                "  • if you have not offered competitor analysis yet → ask via the "
                "present_options tool (field \"competitive_analysis\"): \"Want me to "
                "analyze competitors before we set things up?\" with options "
                '[{"label":"Yes","value":"Yes","answer":"accepted"}, '
                '{"label":"No","value":"No","answer":"declined"}]. BOTH answers are '
                "recorded for you automatically - do NOT call set_campaign_spec for "
                "them, and never set a field the user hasn't stated (F17/F12: don't "
                "copy a value into duration/budget/account to 'proceed').\n"
                "  • they want it (yes / go ahead) → run analyze_competitors.\n"
                "  • the reply is unclear or about something ELSE (budget, a named competitor) "
                "→ re-ask the same Yes/No present_options; do NOT treat a doubtful reply as a "
                "decline.\n"
                "(These are instructions to CALL tools - never type tool-call syntax into your reply.)"
            )

    # Meta creative inspiration - Meta campaigns are creative-bound, so the
    # competitors' running ads are the seed material. Consent-gated (ad-library
    # credits + vision tokens). Three states, all read from durable signals
    # (slice 1d - the offered marker is deleted): ACCEPTED → the fetch is owed;
    # the ask's rail is open → WAIT (the resume steer owns the reply); else →
    # offer it. Resurface after a digression is capped at once via the resolved
    # predicate's ask-count exhaustion.
    if cctx.is_meta and not cctx.competitor_creatives_offer_resolved:
        fetch_chain = (
            "call `fetch_competitor_creatives`"
            if cctx.competitor_names
            else "run `analyze_competitors`, THEN `fetch_competitor_creatives` "
            "in the same turn"
        )
        if offer_state(cctx.spec, "competitor_creatives") is OfferState.ACCEPTED:
            missing.append(
                "competitor creatives - the user said YES to the offer you "
                f"already made. {fetch_chain} NOW. Do NOT ask again via "
                "present_options - the question was already asked and answered. "
                "(These are instructions to CALL tools - never type tool-call "
                "syntax into your reply.)"
            )
        elif cctx.pending_ask_field == "competitor_creatives":
            pass  # ask is on screen - waiting on the reply, never re-prescribed
        else:
            # "recent", not "running" - the ad library's crawl lags, so what we
            # show may include recently-paused ads (each card carries its own
            # Active/Paused + last-seen chips).
            question = (
                "Want to see your competitors' recent ads?"
                if cctx.competitor_names
                else "Want me to analyze your competitors and show their "
                "recent ads?"
            )
            missing.append(
                "competitor creatives - offer it ONCE: ask via the present_options "
                f'tool (field "competitor_creatives"): "{question}" with options '
                '[{"label":"Yes","value":"Yes","answer":"accepted"}, '
                '{"label":"No","value":"No","answer":"declined"}]. BOTH answers '
                "are recorded for you automatically - do NOT call "
                "set_campaign_spec for them. (This is an instruction to CALL the "
                "tool - never type tool-call syntax into your reply.)"
            )

    if not cctx.spec.get("duration"):
        if cctx.field_asks.get("duration", 0) >= ESCAPE_AFTER_ASKS:
            # R12 · refused-required-slot escape: repeated asks landed nothing.
            missing.append(
                "duration - asked several times without an answer; the user may "
                'be unsure. Offer help via the present_options tool (field '
                '"duration"): "Not sure? Most campaigns start with 30 days - '
                'want to go with that? Or just type your own." with the single '
                'option [{"label":"Yes, use 30 days","value":"30 days",'
                '"answer":"30 days"}]. '
                "NEVER store a duration the user hasn't explicitly picked or "
                "typed - no silent defaults (F17)."
            )
        else:
            missing.append(
                "duration - use the present_options tool (field \"duration\") to ask "
                "\"How long should the campaign run? Pick one below or type your "
                "own.\" with chips 30 days / 60 days / 90 days, each carrying "
                "answer == its value; a typed reply like \"45 days\" is handled "
                "for you. CALL the tool - never type the call into your reply."
            )
    if not cctx.spec.get("budget"):
        currency = "₹" if cctx.is_real_estate else "$"
        if cctx.field_asks.get("budget", 0) >= ESCAPE_AFTER_ASKS:
            recommended = f"{currency}10,000/day"
            missing.append(
                "budget - asked several times without an answer; the user may "
                'be unsure. Offer help via the present_options tool (field '
                f'"budget"): "Not sure? {recommended} is a solid starting point '
                '- want to go with that? Or just type your own." with the single '
                f'option [{{"label":"Yes, use {recommended}","value":"{recommended}",'
                f'"answer":"{recommended}"}}]. '
                "NEVER store a budget the user hasn't explicitly picked or "
                "typed - no silent defaults (F17)."
            )
        else:
            missing.append(
                "budget - use the present_options tool (field \"budget\") to ask "
                "\"What's your daily budget? Pick one below or type your own.\" "
                f"with platform-tuned chips (e.g. {currency}5,000/day, "
                f"{currency}10,000/day, {currency}25,000/day), each carrying "
                'answer == its value; a typed reply like "4k" is handled for '
                "you. CALL the tool - never type the call into your reply."
            )
    # Account-block lines depend on the platform pick - skip until platform
    # is set so we don't suggest the wrong fetch tool.
    if cctx.spec.get("platform"):
        if not cctx.spec.get("parent_account"):
            fetch = (
                "fetch_google_parent_accounts"
                if cctx.is_google
                else "fetch_meta_parent_accounts"
            )
            missing.append(
                f"parent_account - call `{fetch}()` first; the result tells you "
                "the present_options call to make next."
            )
        if not cctx.spec.get("account"):
            fetch = "fetch_google_accounts" if cctx.is_google else "fetch_meta_accounts"
            missing.append(
                f"account - call `{fetch}(parent_id=<stored parent>)`; result tells you "
                "the present_options call."
            )

    if cctx.is_meta:
        if not cctx.spec.get("fb_page"):
            missing.append(
                "fb_page - call `fetch_meta_fb_pages(parent_id=<stored parent>)`; "
                "result tells you the present_options call."
            )
        # v3 · F3 - Instagram is OPTIONAL (Facebook-only is a valid campaign).
        # Offer it once; honour skip/later; never block. Gated on fb_page being
        # set so we ask one thing at a time.
        elif (
            not cctx.spec.get("ig_page")
            and offer_state(cctx.spec, "instagram") is not OfferState.DECLINED
        ):
            if is_ig_skip(cctx.last_user):
                missing.append(
                    "instagram - user is skipping Instagram (it's OPTIONAL). Call "
                    '`set_campaign_spec(instagram="declined")` and proceed to review.'
                )
            elif cctx.ig_accounts_fetched:
                # Already FETCHED - do NOT re-fetch (that was the live loop).
                # v5: fetch-time ≠ render-time. The marker is set when the fetch
                # tool returns, but the model may not have rendered the choice
                # yet - claiming "options are on screen" made it skip
                # present_options AND tell the user to click chips that didn't
                # exist. Prescribe the render instead of assuming it.
                missing.append(
                    "instagram - Instagram accounts were already fetched; do NOT call "
                    "fetch_meta_ig_accounts again. If you have NOT yet shown the choice, "
                    "call present_options EXACTLY as the fetch result instructed. "
                    "If the user picked an account it's captured. If they want "
                    'Facebook only, call `set_campaign_spec(instagram="declined")`. '
                    "If they're connecting an Instagram account, wait and re-fetch "
                    "only when they say they're ready."
                )
            else:
                missing.append(
                    "ig_page - Instagram is OPTIONAL. Call "
                    "`fetch_meta_ig_accounts(page_id=<stored fb_page>)`; the result tells you "
                    'the present_options call (it includes a "Continue with Facebook only" '
                    "option). If none are linked, the tool says so - offer Facebook-only."
                )

    if not missing:
        meta_extra = ""
        if cctx.is_meta:
            meta_extra = "\n  - **Facebook Page**: <copy verbatim from State, including '(ID: …)'>"
            meta_extra += (
                "\n  - **Instagram Account**: <copy verbatim from State, including '(ID: …)'>"
                if cctx.spec.get("ig_page")
                else "\n  - **Instagram Account**: not linked (Facebook only)"
            )
        missing.append(
            "review & publish - TWO separate steps this turn:\n"
            "(1) Your TEXT reply is EXACTLY this markdown summary, with values copied "
            "VERBATIM from the `## State` block above (do NOT rephrase, do NOT drop "
            "fields, do NOT replace IDs with placeholders like 'Linked' or 'Connected', "
            "do NOT abbreviate):\n\n"
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
            "  - **Competitors**: <comma-separated names from State, or 'none analyzed' "
            "if competitor_analysis_attempted is true with empty list, or 'declined' "
            "if competitive analysis was declined>\n\n"
            "EVERY bullet must be present - do not omit any.\n"
            "(2) THEN, separately, use the present_options tool to ask \"Ready to launch "
            "the campaign?\" with chips: Yes, launch / No, make changes. When the user "
            "picks 'Yes, launch', run the launch_campaign tool (no arguments) - the one "
            "tool that persists the campaign. These are tools to CALL - never type "
            "tool-call syntax into your reply, only the markdown summary above is text."
        )

    return missing
