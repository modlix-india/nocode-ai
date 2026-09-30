"""The adzump orchestrator's journey - pure decision logic.

``AdzumpContext`` is a typed, frozen read-model over ``session.context``,
shared by every journey. ``NEW_CAMPAIGN`` declares the campaign build as
ordered, dependency-typed ``Step``s (the engine is core/journey.py; optimize/
insights journeys plug in beside it): per step, the exact tool call it
prescribes while owed and the value it shows in State. All pure functions -
no I/O, no session mutation - the most test-valuable code in the
orchestrator lives in a leaf module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field as dc_field
from typing import Callable

from app.core.session import BaseSession
from app.agents.adzump.agents.campaign.google.keyword.themes import (
    DEFAULT_THEME_IDS,
    KEYWORD_THEMES,
)
from app.agents.adzump.agents.campaign.models import (
    Channel,
    build_gaps,
    build_review_items,
    is_build_complete,
    resolve_channel,
)
from app.agents.adzump.core.dynamic_context import DynamicContext
from app.agents.adzump.core.journey import Journey, Step
from app.agents.adzump.models import (
    LEGACY_DECLINED_KEYS,
    LocationProposal,
    OfferResolution,
    OfferState,
    competitor_profiles,
    offer_state,
)
from app.agents.adzump.platform import (
    Platform,
    is_google as _platform_is_google,
    is_mapped_for,
    is_meta as _platform_is_meta,
)
from app.agents.adzump.tools.campaign_data import (
    CREATIVES_REVIEW_ASK,
    _last_user_text,
    analysis_offer_resolution,
    creatives_offer_resolution,
    instagram_offer_resolution,
    is_ig_skip,
    is_real_estate,
)


# A required slot asked this many times without landing switches to the
# "help me pick" escape - the user may be unsure; never a silent default.
ESCAPE_AFTER_ASKS = 3
_MAX_COMPETITORS_SHOWN = 5
# The State value of a settled Meta creatives offer (OPEN shows nothing yet).
_CREATIVES_VERDICT = {
    OfferResolution.DECLINED: "declined",
    OfferResolution.FULFILLED: "fetched",
    OfferResolution.MOOT: "no competitors to fetch for",
    OfferResolution.EXHAUSTED: "not answered - skipped",
}
# A refusal, however the chip was labelled or answered.
_CONSENT_REFUSALS = ("false", "no")


@dataclass(frozen=True)
class AdzumpContext:
    """Typed read-model over ``session.context``.

    Shields the journey engine and the renderers from raw-dict shape drift.
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
    # True once fetch_meta_ig_accounts stored its result (the ``ig_accounts``
    # data key, [] when none are linked). Stops the instagram step from
    # re-prescribing the IG fetch every turn.
    ig_accounts_fetched: bool = False
    # The open elicitation's field, if any - the CURRENT ask on screen. An
    # offer whose rail is open is waiting on the reply, never re-prescribed.
    pending_ask_field: str | None = None
    # Times each field-tagged ask has been shown (present_options counter).
    # Drives offer exhaustion and the ESCAPE_AFTER_ASKS escape.
    field_asks: dict[str, int] = dc_field(default_factory=dict)
    # WHY the Meta creative-inspiration offer is settled (or OPEN = still
    # owed) - the shared verdict from campaign_data, so this gate, the review
    # gate, and the turn record can never disagree. Defaulted for direct test
    # construction.
    competitor_creatives_resolution: OfferResolution = OfferResolution.OPEN
    # The tool whose question the user's message answers: its ask fills
    # `user_message`, so the reply goes back to it untouched. Read before
    # _resume_elicitation_section pops the ask later in the same build.
    awaiting_tool: str | None = None
    # True once the user okays the campaign summary - the gate before anything
    # is built or launched.
    summary_confirmed: bool = False
    # The Google build, from the channel that owns it (agents/campaign/models.py):
    #   build_done    every slot it can't launch without is filled, no work left
    #   build_gaps    work a filled slot still owes, each naming its repair tool
    #   review_items  what the review panel shows, named by the channel
    build_done: bool = False
    build_gaps: tuple[str, ...] = ()
    review_items: tuple[str, ...] = ()

    @classmethod
    def from_session(cls, session: BaseSession) -> "AdzumpContext":
        ctx = session.context
        competitive_raw = ctx.get("competitor_analysis")
        # Sole writer (tools/location.py) stores a LocationProposal.
        proposal = LocationProposal.from_stored(ctx.get("_pending_location_confirm"))
        pending_location = proposal.address if proposal else None
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
                p.name for p in competitor_profiles(ctx) if p.name
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
            competitor_creatives_resolution=creatives_offer_resolution(
                ctx.get("campaign_spec") or {}, ctx
            ),
            awaiting_tool=pe.get("tool") if pe.get("field") == "user_message" else None,
            summary_confirmed=_is_affirmative(
                (ctx.get("campaign_spec") or {}).get("summary_confirmed")),
            build_done=is_build_complete(ctx),
            build_gaps=build_gaps(ctx),
            review_items=build_review_items(ctx),
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
    def channel(self) -> Channel:
        """Google's campaign type. Meaningless for Meta - guard with ``is_google``."""
        return resolve_channel(self.spec)

    @property
    def has_mapped_geo_targets(self) -> bool:
        return is_mapped_for(
            self.product.get("target_areas"), self.spec.get("platform")
        )


def _is_affirmative(value: object) -> bool:
    """Whether a captured consent answer means yes. A chip stores "true"; a typed
    reply the model records ("yes, go ahead") must read the same way."""
    text = str(value or "").strip().lower()
    return bool(text) and not text.startswith(_CONSENT_REFUSALS)


# ─── Step prescriptions ──────────────────────────────────────────────────────

def _prescribe_product(actx: AdzumpContext) -> str:
    # No lead-in: "I'll analyze the website now" read wrong when the tool then
    # reused the saved profile instead (live 2026-09-23).
    return ("business URL - call `analyze_product(url=<the user's URL>)` with no "
            "lead-in text; its result says whether it analyzed or reused saved data")


def _prescribe_location(actx: AdzumpContext) -> str:
    if actx.pending_location:
        # Map shown last turn. Branch on user reply.
        detected = actx.pending_location
        return (
            f"location - map shown for **'{detected}'**. "
            f'If user said `"confirm"` → `set_campaign_spec(location="{detected}")`. '
            f'If JSON `{{"type":"location_update","address":"X",...}}` → '
            f'`set_campaign_spec(location="X")` (use address; fall back to '
            f'`"{detected}"`). '
            f"If user said WRONG/INCORRECT/NOT RIGHT → call `confirm_location()` again. "
            f"If user named a DIFFERENT city → `set_campaign_spec(location=<what they said>)`."
        )
    return "location - call `confirm_location()` (real estate business)"


def _prescribe_platform(actx: AdzumpContext) -> str:
    return (
        "platform - use the present_options tool (field \"platform\") to ask "
        "\"Which platform should we run this on?\" with chips Google Ads / "
        "Meta, each carrying answer == its value. CALL the tool - never "
        "type the call into your reply."
    )


def _prescribe_target_areas(actx: AdzumpContext) -> str:
    loc_arg = actx.spec.get("location") or ""
    return (
        'target_areas - call `manage_targeting_locations(user_message="set up geo targeting")`'
        if not loc_arg
        else f'target_areas - call `manage_targeting_locations(user_message="set up geo targeting for {loc_arg!r}")`'
    )


def _prescribe_competitive_analysis(actx: AdzumpContext) -> str:
    # Agentic, not a hardcoded phrase ladder: the MODEL interprets the user's
    # reply to THIS competitor offer (an exact-match list misses phrasings like
    # "No, skip competitor analysis for now" and re-asks forever). Biased to
    # re-ask on doubt so a polarity-flip ("no, change the budget") is never
    # read as a decline. The _field_traceable guard backstops it.
    if offer_state(actx.spec, "competitive_analysis") is OfferState.ACCEPTED:
        # Every chip writes: the Yes already landed as ACCEPTED - the ask
        # is settled, only the analysis itself is owed.
        return (
            "competitive analysis - the user already said YES to it. Run "
            "`analyze_competitors` NOW; do NOT re-ask. (This is an "
            "instruction to CALL the tool - never type tool-call syntax "
            "into your reply.)"
        )
    return (
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


def _prescribe_competitor_creatives(actx: AdzumpContext) -> str:
    # Meta campaigns are creative-bound, so the competitors' running ads are
    # the seed material. Consent-gated (ad-library credits + vision tokens).
    if offer_state(actx.spec, "competitor_creatives") is OfferState.ACCEPTED:
        if not actx.competitor_names:
            return (
                "competitor creatives - the user said YES. Run "
                "`analyze_competitors` NOW. Do NOT fetch creatives in the "
                "same turn: once the list posts, the user REVIEWS it "
                "(add/update/delete) before any credits are spent. (This is "
                "an instruction to CALL the tool - never type tool-call "
                "syntax into your reply.)"
            )
        return (
            "competitor creatives - consented, and the competitor list is on "
            f"screen. {CREATIVES_REVIEW_ASK} On their go-ahead call "
            "`fetch_competitor_creatives`. (These are instructions to CALL "
            "tools - never type tool-call syntax into your reply.)"
        )
    # "recent", not "running" - the search returns recently-paused ads too
    # (each card carries its own Active/Paused + last-seen chips).
    question = (
        "Want to see your competitors' recent ads?"
        if actx.competitor_names
        else "Want me to analyze your competitors and show their "
        "recent ads?"
    )
    return (
        "competitor creatives - offer it ONCE: ask via the present_options "
        f'tool (field "competitor_creatives"): "{question}" with options '
        '[{"label":"Yes","value":"Yes","answer":"accepted"}, '
        '{"label":"No","value":"No","answer":"declined"}]. A chip answer '
        "is recorded for you automatically; if the user TYPES their answer, "
        "record it with set_campaign_spec(competitor_creatives=\"accepted\" "
        "or \"declined\"). (This is an instruction to CALL the tool - never "
        "type tool-call syntax into your reply.)"
    )


def _prescribe_duration(actx: AdzumpContext) -> str:
    if actx.field_asks.get("duration", 0) >= ESCAPE_AFTER_ASKS:
        # Refused-required-slot escape: repeated asks landed nothing.
        return (
            "duration - asked several times without an answer; the user may "
            'be unsure. Offer help via the present_options tool (field '
            '"duration"): "Not sure? Most campaigns start with 30 days - '
            'want to go with that? Or just type your own." with the single '
            'option [{"label":"Yes, use 30 days","value":"30 days",'
            '"answer":"30 days"}]. '
            "NEVER store a duration the user hasn't explicitly picked or "
            "typed - no silent defaults (F17)."
        )
    return (
        "duration - use the present_options tool (field \"duration\") to ask "
        "\"How long should the campaign run? Pick one below or type your "
        "own.\" with chips 30 days / 60 days / 90 days, each carrying "
        "answer == its value; a typed reply like \"45 days\" is handled "
        "for you. CALL the tool - never type the call into your reply."
    )


def _prescribe_budget(actx: AdzumpContext) -> str:
    currency = "₹" if actx.is_real_estate else "$"
    if actx.field_asks.get("budget", 0) >= ESCAPE_AFTER_ASKS:
        recommended = f"{currency}10,000/day"
        return (
            "budget - asked several times without an answer; the user may "
            'be unsure. Offer help via the present_options tool (field '
            f'"budget"): "Not sure? {recommended} is a solid starting point '
            '- want to go with that? Or just type your own." with the single '
            f'option [{{"label":"Yes, use {recommended}","value":"{recommended}",'
            f'"answer":"{recommended}"}}]. '
            "NEVER store a budget the user hasn't explicitly picked or "
            "typed - no silent defaults (F17)."
        )
    return (
        "budget - use the present_options tool (field \"budget\") to ask "
        "\"What's your daily budget? Pick one below or type your own.\" "
        f"with platform-tuned chips (e.g. {currency}5,000/day, "
        f"{currency}10,000/day, {currency}25,000/day), each carrying "
        'answer == its value; a typed reply like "4k" is handled for '
        "you. CALL the tool - never type the call into your reply."
    )


def _prescribe_parent_account(actx: AdzumpContext) -> str:
    fetch = (
        "fetch_google_parent_accounts"
        if actx.is_google
        else "fetch_meta_parent_accounts"
    )
    return (
        f"parent_account - call `{fetch}()` first; the result tells you "
        "the present_options call to make next."
    )


def _prescribe_account(actx: AdzumpContext) -> str:
    fetch = "fetch_google_accounts" if actx.is_google else "fetch_meta_accounts"
    return (
        f"account - call `{fetch}(parent_id=<stored parent>)`; result tells you "
        "the present_options call."
    )


def _prescribe_fb_page(actx: AdzumpContext) -> str:
    return (
        "fb_page - call `fetch_meta_fb_pages(parent_id=<stored parent>)`; "
        "result tells you the present_options call."
    )


def _prescribe_instagram(actx: AdzumpContext) -> str:
    # Instagram is OPTIONAL (Facebook-only is a valid campaign). Offer it
    # once; honour skip/later; never block.
    if is_ig_skip(actx.last_user):
        return (
            "instagram - user is skipping Instagram (it's OPTIONAL). Call "
            '`set_campaign_spec(instagram="declined")` and proceed to review.'
        )
    if actx.ig_accounts_fetched:
        # Already FETCHED - do NOT re-fetch (re-fetching loops). But
        # fetch-time ≠ render-time: the marker is set when the fetch tool
        # returns, and the model may not have rendered the choice yet -
        # assuming "options are on screen" makes it skip present_options and
        # tell the user to click chips that don't exist. Prescribe the render
        # instead of assuming it.
        return (
            "instagram - Instagram accounts were already fetched; do NOT call "
            "fetch_meta_ig_accounts again. If you have NOT yet shown the choice, "
            "call present_options EXACTLY as the fetch result instructed. "
            "If the user picked an account it's captured. If they want "
            'Facebook only, call `set_campaign_spec(instagram="declined")`. '
            "If they're connecting an Instagram account, wait and re-fetch "
            "only when they say they're ready."
        )
    return (
        "ig_page - Instagram is OPTIONAL. Call "
        "`fetch_meta_ig_accounts(page_id=<stored fb_page>)`; the result tells you "
        'the present_options call (it includes a "Continue with Facebook only" '
        "option). If none are linked, the tool says so - offer Facebook-only."
    )


def _prescribe_tool_reply(actx: AdzumpContext) -> str:
    # First, not only: anything else at the top reads as leave to move on and
    # to report an outcome nobody gave us. The rest stays for a user who
    # changed the subject.
    return (
        f"answer the question `{actx.awaiting_tool}` asked - call "
        f"`{actx.awaiting_tool}(user_message=<their verbatim reply>)` NOW. Do NOT act "
        "on it yourself, claim anything changed, or move on."
    )


def _options(*chips: tuple[str, str | None]) -> str:
    """(label, answer) pairs as the present_options JSON the prescriptions quote."""
    return json.dumps([{"label": label, "value": label, "answer": answer}
                       for label, answer in chips], ensure_ascii=False)


# Asked right after the summary card; show_campaign_summary's result repeats it.
SUMMARY_CONFIRM_ASK = (
    'use the present_options tool (field "summary_confirmed") to ask "Proceed '
    f'with the campaign?" with options {_options(("Yes, proceed", "true"), ("No, make changes", None))}'
)

# The review card is CODE-rendered (tools/summary.py) - the model never
# writes the summary itself, only a lead-in line.
_SUMMARY_PRESCRIPTION = (
    "review the summary - TWO tool calls this turn:\n"
    "(1) call `show_campaign_summary()` - it renders the campaign summary "
    "card for the user from stored state. NEVER write the summary yourself; "
    "a brief lead-in line (\"Everything's set - here's the plan:\") is fine.\n"
    f"(2) THEN {SUMMARY_CONFIRM_ASK}. These are tools to CALL - never type "
    "tool-call syntax into your reply."
)


def _prescribe_channel(actx: AdzumpContext) -> str:
    # From the enum: a channel that exists can be built, so a new one is
    # offered without touching this module.
    options = _options(*((c.chip_label, c.value) for c in Channel))
    return (
        'channel - use the present_options tool (field "channel") to ask "What '
        f'kind of Google campaign should we run?" with options {options}. CALL '
        "the tool - never type the call into your reply."
    )


def _prescribe_ad_groups(actx: AdzumpContext) -> str:
    # One chip per keyword theme plus a combined one, from the theme registry,
    # so a new theme becomes a chip without touching this copy.
    ids = list(DEFAULT_THEME_IDS)
    combined = "Both" if len(ids) == 2 else "All"
    options = _options(*((KEYWORD_THEMES[t].label, t) for t in ids),
                       (combined, ",".join(ids)))
    return (
        'ad groups - use the present_options tool (field "ad_groups") to ask '
        f'"Which ad groups should we build?" with options {options}. Whatever '
        "they pick is what gets built - do not talk them out of narrowing it. "
        "CALL the tool - never type the call into your reply."
    )


def _prescribe_build(actx: AdzumpContext) -> str:
    if actx.build_gaps:
        # A build that ran owes this work; building again would discard it.
        return "\n".join(actx.build_gaps)
    if actx.channel is Channel.SEARCH:
        return (
            "build the campaign - the user okayed the summary and chose the ad groups "
            f'("{actx.spec.get("ad_groups")}"). Call the prepare_campaign_review tool (no '
            "arguments) NOW - it researches the keywords and shows them in the review "
            "panel. Do NOT re-post the summary and do NOT ask either question again."
        )
    return (
        "build the campaign - the user okayed the summary. Call the "
        "prepare_campaign_review tool (no arguments) NOW - it builds the audience "
        "targeting and shows it in the review panel. Do NOT re-post the summary."
    )


_LAUNCH_PRESCRIPTION = (
    "launch - the campaign is confirmed. If State shows a Review panel, ask the "
    "user to review and edit what it shows first. Then use the present_options "
    "tool to ask \"Ready to launch the campaign?\" with chips: Yes, launch / No, "
    "make changes. When the user picks 'Yes, launch', run the launch_campaign "
    "tool (no arguments) - the one tool that persists the campaign. These are "
    "tools to CALL - never type tool-call syntax into your reply."
)


# ─── State values ────────────────────────────────────────────────────────────
# What each step shows in the State section; None = nothing yet.

def website_display(actx: AdzumpContext) -> str:
    """The analyzed business URL - the ONE fallback chain (profile url ->
    first analyzed page -> '-') shared by the State block and the review card
    so the two renderings can never drift."""
    return (
        actx.product_profile.get("url")
        or (actx.product.get("pages_analyzed") or [None])[0]
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


def _product_value(actx: AdzumpContext) -> str | None:
    if not actx.product:
        return None
    name = actx.product.get("product_name") or ""
    if business_type := actx.product.get("business_type"):
        name = f"{name} ({business_type})".strip()
    website = website_display(actx)
    return f"{name or '(unnamed)'}, {website}" if website != "-" else name or "(unnamed)"


def _spec_value(field: str) -> Callable[[AdzumpContext], str | None]:
    return lambda actx: actx.spec.get(field) or None


def _account_value(field: str) -> Callable[[AdzumpContext], str | None]:
    def value(actx: AdzumpContext) -> str | None:
        acct_id = actx.spec.get(field)
        return account_display(acct_id, actx.account_names, actx.spec.get("platform")) if acct_id else None
    return value


def _target_areas_value(actx: AdzumpContext) -> str | None:
    names = [area["name"] for area in actx.product.get("target_areas") or [] if area.get("name")]
    return ", ".join(names) or None


def _competitors_value(actx: AdzumpContext) -> str | None:
    if names := actx.competitor_names:
        hidden = len(names) - _MAX_COMPETITORS_SHOWN
        more = f" (+{hidden} more)" if hidden > 0 else ""
        return ", ".join(names[:_MAX_COMPETITORS_SHOWN]) + more
    if offer_state(actx.spec, "competitive_analysis") is OfferState.DECLINED:
        return "declined"
    return "none analyzed" if actx.competitor_analysis_attempted else None


def _creatives_value(actx: AdzumpContext) -> str | None:
    return _CREATIVES_VERDICT.get(actx.competitor_creatives_resolution)


def _channel_value(actx: AdzumpContext) -> str | None:
    channel = Channel.from_value(actx.spec.get("channel"))
    return channel.chip_label if channel else None


def _instagram_value(actx: AdzumpContext) -> str | None:
    if linked := _account_value("ig_page")(actx):
        return linked
    if instagram_offer_resolution(actx.spec) is OfferResolution.DECLINED:
        return "not linked (Facebook only)"
    return None


# ─── Journey registry ────────────────────────────────────────────────────────
# Coordinates restored from storage count as a valid geo anchor for target-area
# discovery (manage_targeting_locations falls back to product_data.place
# lat/lng), so no fresh confirm is needed to prescribe it.

def _has_geo_anchor(actx: AdzumpContext) -> bool:
    place = actx.product.get("place") or {}
    return bool(actx.spec.get("location")) or place.get("lat") is not None


# Sample State this journey renders (Meta, mid-flow). Rows follow step order;
# off/blocked steps show only when they hold a value (Instagram waits behind
# Facebook Page, Competitors is off on Meta but still lists the rivals):
#
#   ## State
#   - Product: Prestige Lakeside Habitat (Residential Real Estate), https://www.prestigeconstructions.com/lakeside-habitat ✓
#   - Location: Whitefield, Bengaluru ✓ - set 3 turns ago
#   - Platform: Meta Ads ✓ - set 1 turn ago
#   - Target Areas: Whitefield, Varthur ✓
#   - Competitors: Sobha Dream Acres, Brigade Utopia
#   - Competitor ads: fetched ✓ - set 1 turn ago
#   - Duration: 30 days ✓ - just set
#   - Daily Budget: -
#   - Manager / Business Account: Prestige Group (ID: 1029384756) ✓ - just set
#   - Ad Account: Prestige Leads (ID: act_5566778899) ✓ - just set
#   - Facebook Page: -
_DETAILS = (
    Step("product", label="Product", value=_product_value,
         done=lambda actx: bool(actx.product),
         prescribe=_prescribe_product),
    Step("location", label="Location", value=_spec_value("location"),
         fields=("location",), requires=("product",),
         applies=lambda actx: actx.is_real_estate,
         done=lambda actx: bool(actx.spec.get("location")),
         prescribe=_prescribe_location),
    Step("platform", label="Platform", value=_spec_value("platform"),
         fields=("platform",), requires=("product",),
         done=lambda actx: actx.is_google or actx.is_meta,  # one we can launch on
         prescribe=_prescribe_platform),
    Step("target_areas", label="Target Areas", value=_target_areas_value,
         requires=("product", "platform"),
         applies=_has_geo_anchor,
         done=lambda actx: actx.has_mapped_geo_targets,
         prescribe=_prescribe_target_areas),
    # Off on Meta, where analysis runs inside the creatives offer - its row
    # still shows the competitor list there.
    Step("competitive_analysis", label="Competitors", value=_competitors_value,
         fields=("competitive_analysis",), requires=("product",),
         applies=lambda actx: actx.is_google,
         done=lambda actx: analysis_offer_resolution(
             actx.spec, actx.competitor_analysis_attempted)
         is not OfferResolution.OPEN,
         prescribe=_prescribe_competitive_analysis),
    Step("competitor_creatives", label="Competitor ads", value=_creatives_value,
         fields=("competitor_creatives",), requires=("product",),
         applies=lambda actx: actx.is_meta,
         done=lambda actx: actx.competitor_creatives_resolution
         is not OfferResolution.OPEN,
         # An open rail waits on the reply - EXCEPT once the Yes landed as
         # ACCEPTED (a capture can store it before the answered rail is
         # reaped): the fetch is owed NOW.
         ready=lambda actx: (
             actx.pending_ask_field != "competitor_creatives"
             or offer_state(actx.spec, "competitor_creatives")
             is OfferState.ACCEPTED),
         prescribe=_prescribe_competitor_creatives),
    Step("duration", label="Duration", value=_spec_value("duration"),
         fields=("duration",), requires=("product",),
         done=lambda actx: bool(actx.spec.get("duration")),
         prescribe=_prescribe_duration),
    Step("budget", label="Daily Budget", value=_spec_value("budget"),
         fields=("budget",), requires=("product",),
         done=lambda actx: bool(actx.spec.get("budget")),
         prescribe=_prescribe_budget),
    Step("parent_account", label="Manager / Business Account",
         value=_account_value("parent_account"),
         fields=("parent_account",), requires=("product", "platform"),
         done=lambda actx: bool(actx.spec.get("parent_account")),
         prescribe=_prescribe_parent_account),
    Step("account", label="Ad Account", value=_account_value("account"),
         fields=("account",), requires=("product", "platform"),
         done=lambda actx: bool(actx.spec.get("account")),
         prescribe=_prescribe_account),
    Step("fb_page", label="Facebook Page", value=_account_value("fb_page"),
         fields=("fb_page",), requires=("product",),
         applies=lambda actx: actx.is_meta,
         done=lambda actx: bool(actx.spec.get("fb_page")),
         prescribe=_prescribe_fb_page),
    Step("instagram", label="Instagram Account", value=_instagram_value,
         fields=("ig_page", "instagram"), requires=("product", "fb_page"),
         applies=lambda actx: actx.is_meta,
         done=lambda actx: instagram_offer_resolution(actx.spec)
         is not OfferResolution.OPEN,
         prescribe=_prescribe_instagram),
)

# The details alone: complete = the summary card may render.
CAMPAIGN_DETAILS = Journey(name="CAMPAIGN_DETAILS", steps=_DETAILS,
                           finish=_SUMMARY_PRESCRIPTION)

# The whole flow: a helper's open question first, then the details, the
# summary okay, and on Google the build the review panel shows.
NEW_CAMPAIGN = Journey(name="NEW_CAMPAIGN", finish=_LAUNCH_PRESCRIPTION, steps=(
    Step("tool_question", label="Question from a helper", value=lambda actx: actx.awaiting_tool,
         applies=lambda actx: actx.awaiting_tool is not None,
         done=lambda actx: False,
         prescribe=_prescribe_tool_reply),
    *_DETAILS,
    Step("summary", label="Campaign summary",
         value=lambda actx: "confirmed" if actx.summary_confirmed else None,
         fields=("summary_confirmed",), requires=tuple(step.name for step in _DETAILS),
         done=lambda actx: actx.summary_confirmed,
         prescribe=lambda actx: _SUMMARY_PRESCRIPTION),
    # A build that ran has answered channel and ad groups already (an old
    # session may hold one without either in the spec).
    Step("channel", label="Campaign type", value=_channel_value,
         fields=("channel",), requires=("summary",),
         applies=lambda actx: actx.is_google,
         done=lambda actx: bool(actx.spec.get("channel") or actx.build_done
                                or actx.build_gaps),
         prescribe=_prescribe_channel),
    # Ad groups are keyword themes, so only Search picks them.
    Step("ad_groups", label="Ad groups", value=_spec_value("ad_groups"),
         fields=("ad_groups",), requires=("channel",),
         applies=lambda actx: actx.is_google and actx.channel is Channel.SEARCH,
         done=lambda actx: bool(actx.spec.get("ad_groups") or actx.build_done
                                or actx.build_gaps),
         prescribe=_prescribe_ad_groups),
    Step("build", label="Review panel",
         value=lambda actx: ", ".join(actx.review_items) or None,
         requires=("channel", "ad_groups"),
         applies=lambda actx: actx.is_google,
         done=lambda actx: actx.build_done,
         prescribe=_prescribe_build),
))


# ─── The orchestrator's dynamic context ─────────────────────────────────────
# Its journey plus its own fixed text; core/dynamic_context.py renders the reminder.

_OPTIONS_NOT_VALUES = (
    "Example values below (e.g. \"30 days\", \"₹5,000/day\") are OPTIONS to "
    "SHOW the user via present_options - NEVER values to store. Only "
    "`set_campaign_spec` a field after the user actually states it (F12)."
)

_HOW_TO_RESPOND = (
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
    "1b. ONCE THE KEYWORD PANEL EXISTS - a question about its keywords, an edit, or "
    "retrying an ad group that failed → call `manage_keywords(user_message=<their "
    "verbatim message>)` IMMEDIATELY. Do NOT answer it yourself - the keyword agent "
    "recorded why each keyword was chosen or skipped; you did not, so you would be "
    "guessing. Before the panel exists this rule does not apply: choosing which ad "
    "groups to build is a `set_campaign_spec(ad_groups=...)` answer, not a keyword "
    "edit.\n"
    "   A Demand Gen custom segment is also built from keywords - that is rule 1c, not "
    "this one.\n"
    '   Triggers (any of these): why a keyword is there ("why did you include '
    'affordable running shoes?"); why one ISN\'T ("why is cheap shoes missing?", '
    '"where\'s my brand name?"); judgement on a keyword ("is X too broad?", '
    '"is X worth it?"); adding ("add keywords for the locations", "include '
    'apartment terms too"); removing or changing ("remove the low-volume ones", '
    '"make X exact match", "that one\'s irrelevant").\n'
    "   AFTER manage_keywords: the keyword agent has ALREADY replied to the user in chat, "
    "and you were NOT told what it did. Do NOT restate it, summarise it, or claim any "
    "outcome (added / removed / changed) - you would be guessing. Just continue.\n"
    "1c. Audience question or edit (once an audience is in the panel) → call "
    "`manage_audience(user_message=<their verbatim message>)` IMMEDIATELY. Do NOT "
    "answer it yourself - the audience agent recorded why each segment was chosen and "
    "holds Google's segment catalogue; you have neither, so you would be guessing.\n"
    '   Triggers (any of these): why a segment is targeted ("why are we targeting '
    'apartment buyers?"); who it reaches ("who does this actually reach?"); adding '
    '("add something for new parents", "target people moving house"); removing '
    '("drop the luxury one", "stop targeting students"); demographic changes '
    '("only 25 to 44", "women only", "top income brackets").\n'
    "   AFTER manage_audience: same rule as 1b - the audience agent has already replied "
    "and you were NOT told what it changed. Do NOT restate or claim any outcome.\n"
    "2. Info question → answer briefly from State, then do the Next action.\n"
    "3. Correction → `set_campaign_spec(<field>=<new>)`, acknowledge, then re-check Next action.\n"
    "4. **New data** (typed or chip-clicked) → `set_campaign_spec(<field>=<value>)` IMMEDIATELY, "
    "even if the value is for a different field than Next action. "
    'Examples: user says "Google Ads" → `set_campaign_spec(platform="Google Ads")`. '
    'User says "₹10,000/day" → `set_campaign_spec(budget="₹10,000/day")`. '
    "Then acknowledge in one short sentence and re-check Next action.\n"
    '5. Ambient ("ok", "continue", "next") → just do Next action.\n'
    "6. Otherwise → do Next action.\n"
    "\n**You report tool results.** Tools and helper agents never write to "
    "the chat - their results reach only you (except the keyword and audience "
    "agents, rules 1b/1c). Tell the user what changed "
    "(competitors found, ads fetched, targeting updated, images saved) "
    "once, briefly, in your own words; the side panel already shows the "
    "full details. Only a question widget or the campaign summary card "
    "shows itself - never repeat those.\n"
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

ORCHESTRATOR_CONTEXT = DynamicContext(
    journey=NEW_CAMPAIGN,
    reply_rules=_HOW_TO_RESPOND,
    missing_note=_OPTIONS_NOT_VALUES,
)
