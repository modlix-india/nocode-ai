"""The adzump orchestrator's workflow tree - pure decision logic.

``CampaignContext`` is a typed, frozen read-model over ``session.context``.
The ``NEW_CAMPAIGN`` journey declares the build as ordered, dependency-typed
``Step``s; ``missing_list`` walks them and emits the ordered prescriptions
(with the exact tool call per item). All pure functions - no I/O, no session
mutation - the most test-valuable code in the orchestrator lives in a leaf
module.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from app.core.session import BaseSession
from app.agents.adzump.models import (
    LEGACY_DECLINED_KEYS,
    OfferState,
    competitor_profiles,
    offer_state,
)
from app.agents.adzump.platform import (
    is_google as _platform_is_google,
    is_mapped_for,
    is_meta as _platform_is_meta,
)
from typing import Callable

from app.agents.adzump.tools.campaign_data import (
    _last_user_text,
    competitor_creatives_offer_resolved,
    is_ig_skip,
    is_real_estate,
)


# R12 · a required slot asked this many times without landing switches to the
# "help me pick" escape - the user may be unsure; never a silent default (F17).
ESCAPE_AFTER_ASKS = 3


@dataclass(frozen=True)
class CampaignContext:
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
    # v3 · F3 - True once fetch_meta_ig_accounts stored its result (the
    # ``ig_accounts`` data key, [] when none are linked). Stops the instagram step
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


# ─── The dependency engine (slice 3) ────────────────────────────────────────

@dataclass(frozen=True)
class Step:
    """One unit of the journey. ``done`` says the step is satisfied;
    ``applies`` scopes it (a non-applicable step is invisible AND satisfies
    dependents); ``requires`` names earlier steps that must be done first
    (unmet -> the step is hidden, not skipped); ``ready`` is the waiting
    gate - False means an ask is in flight: never re-prescribed, but the
    journey is NOT complete (waiting, not skipped)."""

    name: str
    done: Callable[[CampaignContext], bool]
    prescribe: Callable[[CampaignContext], str]
    applies: Callable[[CampaignContext], bool] = lambda cctx: True
    ready: Callable[[CampaignContext], bool] = lambda cctx: True
    requires: tuple[str, ...] = ()


@dataclass(frozen=True)
class Journey:
    name: str
    steps: tuple[Step, ...]


def missing_list(journey: Journey, cctx: CampaignContext) -> list[str]:
    """Walk the journey against the typed context; return the ordered
    prescriptions for every actionable not-done step (FIRST is the ask).
    All applicable steps done -> the review prescription. A waiting or
    requires-blocked step contributes no line but blocks completion.

    Each prescription names the exact tool call to make - including a
    suggested ``question`` for chip asks - so the LLM has nothing to
    construct, only to copy.
    """
    applicable = [step for step in journey.steps if step.applies(cctx)]
    satisfied = {step.name for step in journey.steps if not step.applies(cctx)}
    satisfied |= {step.name for step in applicable if step.done(cctx)}

    lines: list[str] = []
    complete = True
    for step in applicable:
        if step.name in satisfied:
            continue
        complete = False
        if any(required not in satisfied for required in step.requires):
            continue  # hidden behind an upstream ask - it is on screen instead
        if not step.ready(cctx):
            continue  # ask in flight - waiting on the reply, never re-prescribed
        lines.append(step.prescribe(cctx))
    if complete:
        lines.append(_REVIEW_PRESCRIPTION)
    return lines


# ─── Step prescriptions (verbatim from the retired if-chain) ─────────────────

def _prescribe_product(cctx: CampaignContext) -> str:
    return "business URL - call `analyze_product(url=<the user's URL>)`"


def _prescribe_location(cctx: CampaignContext) -> str:
    if cctx.pending_location:
        # Map shown last turn. Branch on user reply.
        detected = cctx.pending_location
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


def _prescribe_platform(cctx: CampaignContext) -> str:
    return (
        "platform - use the present_options tool (field \"platform\") to ask "
        "\"Which platform should we run this on?\" with chips Google Ads / "
        "Meta, each carrying answer == its value. CALL the tool - never "
        "type the call into your reply."
    )


def _prescribe_target_areas(cctx: CampaignContext) -> str:
    loc_arg = cctx.spec.get("location") or ""
    return (
        'target_areas - call `manage_targeting_locations(user_message="set up geo targeting")`'
        if not loc_arg
        else f'target_areas - call `manage_targeting_locations(user_message="set up geo targeting for {loc_arg!r}")`'
    )


def _prescribe_competitive_analysis(cctx: CampaignContext) -> str:
    # F11 · agentic, not a hardcoded phrase ladder: the MODEL interprets the
    # user's reply to THIS competitor offer (the old `lu in (...)` exact-match
    # missed "No, skip competitor analysis for now" → re-ask loop). Scoped +
    # biased to re-ask on doubt so a polarity-flip ("no, change the budget")
    # is never read as a decline. The _field_traceable guard backstops it.
    if offer_state(cctx.spec, "competitive_analysis") is OfferState.ACCEPTED:
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


def _prescribe_competitor_creatives(cctx: CampaignContext) -> str:
    # Meta campaigns are creative-bound, so the competitors' running ads are
    # the seed material. Consent-gated (ad-library credits + vision tokens).
    fetch_chain = (
        "call `fetch_competitor_creatives`"
        if cctx.competitor_names
        else "run `analyze_competitors`, THEN `fetch_competitor_creatives` "
        "in the same turn"
    )
    if offer_state(cctx.spec, "competitor_creatives") is OfferState.ACCEPTED:
        return (
            "competitor creatives - the user said YES to the offer you "
            f"already made. {fetch_chain} NOW. Do NOT ask again via "
            "present_options - the question was already asked and answered. "
            "(These are instructions to CALL tools - never type tool-call "
            "syntax into your reply.)"
        )
    # "recent", not "running" - the ad library's crawl lags, so what we
    # show may include recently-paused ads (each card carries its own
    # Active/Paused + last-seen chips).
    question = (
        "Want to see your competitors' recent ads?"
        if cctx.competitor_names
        else "Want me to analyze your competitors and show their "
        "recent ads?"
    )
    return (
        "competitor creatives - offer it ONCE: ask via the present_options "
        f'tool (field "competitor_creatives"): "{question}" with options '
        '[{"label":"Yes","value":"Yes","answer":"accepted"}, '
        '{"label":"No","value":"No","answer":"declined"}]. BOTH answers '
        "are recorded for you automatically - do NOT call "
        "set_campaign_spec for them. (This is an instruction to CALL the "
        "tool - never type tool-call syntax into your reply.)"
    )


def _prescribe_duration(cctx: CampaignContext) -> str:
    if cctx.field_asks.get("duration", 0) >= ESCAPE_AFTER_ASKS:
        # R12 · refused-required-slot escape: repeated asks landed nothing.
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


def _prescribe_budget(cctx: CampaignContext) -> str:
    currency = "₹" if cctx.is_real_estate else "$"
    if cctx.field_asks.get("budget", 0) >= ESCAPE_AFTER_ASKS:
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


def _prescribe_parent_account(cctx: CampaignContext) -> str:
    fetch = (
        "fetch_google_parent_accounts"
        if cctx.is_google
        else "fetch_meta_parent_accounts"
    )
    return (
        f"parent_account - call `{fetch}()` first; the result tells you "
        "the present_options call to make next."
    )


def _prescribe_account(cctx: CampaignContext) -> str:
    fetch = "fetch_google_accounts" if cctx.is_google else "fetch_meta_accounts"
    return (
        f"account - call `{fetch}(parent_id=<stored parent>)`; result tells you "
        "the present_options call."
    )


def _prescribe_fb_page(cctx: CampaignContext) -> str:
    return (
        "fb_page - call `fetch_meta_fb_pages(parent_id=<stored parent>)`; "
        "result tells you the present_options call."
    )


def _prescribe_instagram(cctx: CampaignContext) -> str:
    # v3 · F3 - Instagram is OPTIONAL (Facebook-only is a valid campaign).
    # Offer it once; honour skip/later; never block.
    if is_ig_skip(cctx.last_user):
        return (
            "instagram - user is skipping Instagram (it's OPTIONAL). Call "
            '`set_campaign_spec(instagram="declined")` and proceed to review.'
        )
    if cctx.ig_accounts_fetched:
        # Already FETCHED - do NOT re-fetch (that was the live loop).
        # v5: fetch-time ≠ render-time. The marker is set when the fetch
        # tool returns, but the model may not have rendered the choice
        # yet - claiming "options are on screen" made it skip
        # present_options AND tell the user to click chips that didn't
        # exist. Prescribe the render instead of assuming it.
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


# Slice 2 · the review card is CODE-rendered (tools/summary.py) - the model
# stopped being a template engine; this two-step prescription replaced the
# 35-line "reproduce VERBATIM" template.
_REVIEW_PRESCRIPTION = (
    "review & publish - TWO tool calls this turn:\n"
    "(1) call `show_campaign_summary()` - it renders the campaign summary "
    "card for the user from stored state. NEVER write the summary yourself; "
    "a brief lead-in line (\"Everything's set - here's the plan:\") is fine.\n"
    "(2) THEN use the present_options tool to ask \"Ready to launch the "
    "campaign?\" with chips: Yes, launch / No, make changes. When the user "
    "picks 'Yes, launch', run the launch_campaign tool (no arguments) - the "
    "one tool that persists the campaign. These are tools to CALL - never "
    "type tool-call syntax into your reply."
)


# ─── Journey registry ────────────────────────────────────────────────────────
# NEW_CAMPAIGN in the retired if-chain's statement order. Coordinates restored
# from storage count as a valid geo anchor for target-area discovery
# (manage_targeting_locations falls back to product_data.place lat/lng), so no
# fresh confirm is needed to prescribe it.

def _has_geo_anchor(cctx: CampaignContext) -> bool:
    place = cctx.product.get("place") or {}
    return bool(cctx.spec.get("location")) or place.get("lat") is not None


NEW_CAMPAIGN = Journey(name="NEW_CAMPAIGN", steps=(
    Step("product",
         done=lambda cctx: bool(cctx.product),
         prescribe=_prescribe_product),
    Step("location", requires=("product",),
         applies=lambda cctx: cctx.is_real_estate,
         done=lambda cctx: bool(cctx.spec.get("location")),
         prescribe=_prescribe_location),
    Step("platform", requires=("product",),
         done=lambda cctx: bool(cctx.spec.get("platform")),
         prescribe=_prescribe_platform),
    Step("target_areas", requires=("product", "platform"),
         applies=_has_geo_anchor,
         done=lambda cctx: cctx.has_mapped_geo_targets,
         prescribe=_prescribe_target_areas),
    Step("competitive_analysis", requires=("product",),
         applies=lambda cctx: cctx.is_google,
         done=lambda cctx: cctx.competitor_analysis_attempted
         or offer_state(cctx.spec, "competitive_analysis") is OfferState.DECLINED,
         prescribe=_prescribe_competitive_analysis),
    Step("competitor_creatives", requires=("product",),
         applies=lambda cctx: cctx.is_meta,
         done=lambda cctx: cctx.competitor_creatives_offer_resolved,
         ready=lambda cctx: cctx.pending_ask_field != "competitor_creatives",
         prescribe=_prescribe_competitor_creatives),
    Step("duration", requires=("product",),
         done=lambda cctx: bool(cctx.spec.get("duration")),
         prescribe=_prescribe_duration),
    Step("budget", requires=("product",),
         done=lambda cctx: bool(cctx.spec.get("budget")),
         prescribe=_prescribe_budget),
    Step("parent_account", requires=("product", "platform"),
         done=lambda cctx: bool(cctx.spec.get("parent_account")),
         prescribe=_prescribe_parent_account),
    Step("account", requires=("product", "platform"),
         done=lambda cctx: bool(cctx.spec.get("account")),
         prescribe=_prescribe_account),
    Step("fb_page", requires=("product",),
         applies=lambda cctx: cctx.is_meta,
         done=lambda cctx: bool(cctx.spec.get("fb_page")),
         prescribe=_prescribe_fb_page),
    Step("instagram", requires=("product", "fb_page"),
         applies=lambda cctx: cctx.is_meta,
         done=lambda cctx: bool(cctx.spec.get("ig_page"))
         or offer_state(cctx.spec, "instagram") is OfferState.DECLINED,
         prescribe=_prescribe_instagram),
))
