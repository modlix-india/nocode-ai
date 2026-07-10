"""The adzump orchestrator's workflow tree - pure decision logic.

``CampaignContext`` is a typed, frozen read-model over ``session.context``;
``_next_action`` computes the ordered missing-list (with the exact tool call
to make per item) from it; ``_detect_intent`` recognizes an unambiguous
chip/typed answer for a pending field. All pure functions - no I/O, no
session mutation - split out of agent.py so the most test-valuable code in
the orchestrator lives in a leaf module.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.session import BaseSession
from app.agents.adzump.platform import (
    CANONICAL_LABEL,
    Platform,
    is_google as _platform_is_google,
    is_mapped_for,
    is_meta as _platform_is_meta,
)
from app.agents.adzump.tools.campaign_data import (
    _last_user_text,
    is_ig_skip,
    is_real_estate,
)


def _is_custom_reply(text: str) -> bool:
    """v4 · F10 - did the user pick the "Custom" escape on a chip ask? The chip's
    value is literally "Custom", so a click sends exactly that; a typed "custom
    amount" / "custom budget" also qualifies. Tight on purpose - presets are
    never "custom", so this won't fire on a real value."""
    lu = (text or "").strip().lower()
    return lu == "custom" or lu.startswith("custom")


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
    # v3 · F3 - True once Instagram options have been offered (a multi-IG list
    # was shown, or the empty-IG Facebook-only choice). Stops _next_action from
    # re-prescribing the IG fetch every turn. Defaulted so existing test
    # fixtures that build CampaignContext directly need no change.
    ig_offered: bool = False
    # v4 · F10 - the field ("duration"/"budget") whose chip ask the user
    # escaped via "Custom"; we're now awaiting a typed value for it. Drives the
    # free-text prescription instead of re-rendering the same chips. Defaulted.
    awaiting_custom_field: str | None = None
    # True once create_campaign has run and stored keyword_research - flips the review
    # branch from "build the campaign" (run keyword research) to "launch". Defaulted so
    # existing test fixtures that build CampaignContext directly need no change.
    keyword_research_done: bool = False

    @classmethod
    def from_session(cls, session: BaseSession) -> "CampaignContext":
        ctx = session.context
        competitive_raw = ctx.get("competitor_analysis")
        competitive = competitive_raw or {}
        # Sole writer (tools/location.py) stores the detected location string.
        pending_location = ctx.get("_pending_location_confirm") or None
        # v4 · F10 - the field whose chip ask was escaped via "Custom" (awaiting a
        # typed value). Computed before the return; a conditional expression would
        # evaluate the condition before the walrus and raise UnboundLocalError.
        pe = ctx.get("_pending_elicitation") or {}
        awaiting_custom_field = pe.get("field") if pe.get("awaiting_custom") else None
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
            ig_offered=bool(ctx.get("_ig_offered")),
            awaiting_custom_field=awaiting_custom_field,
            keyword_research_done=bool(ctx.get("keyword_research")),
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


def _detect_intent(cctx: CampaignContext) -> tuple[str, str] | None:
    """Recognize when the user's last message is an obvious answer for a
    pending campaign-spec field. Returns (field, value) to store, or None.

    Conservative: only matches unambiguous cases. Anything subtle (custom
    durations, free-form budgets) is left to the LLM via the default
    missing-list prescription.
    """
    lu = (cctx.last_user or "").strip()
    lu_lower = lu.lower()
    if not lu:
        return None
    spec = cctx.spec

    # Platform: "Google Ads" / "Meta" chip clicks or close natural-language
    # variants. Only fire if platform isn't already stored. Defers keyword
    # classification to app.agents.adzump.platform so all consumers stay
    # aligned on which strings count as which platform.
    if not spec.get("platform"):
        platform = Platform.from_value(lu_lower)
        if platform is not None:
            return ("platform", CANONICAL_LABEL[platform])

    return None


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

    # Intent routing: if the user's last message is a recognizable answer for
    # a pending field, surface "store this NOW" as the top of missing. This
    # prevents the LLM from following the default Next-action prescription
    # while ignoring the user's actual input. (E.g. user clicks "Google Ads"
    # chip while location is still missing - without this, the LLM would
    # call confirm_location and drop platform on the floor.)
    intent = _detect_intent(cctx)
    intent_field: str | None = None
    if intent is not None:
        intent_field, value = intent
        missing.append(
            f'{intent_field} - user said "{cctx.last_user[:40]}". '
            f"Call `set_campaign_spec({intent_field}={value!r})` FIRST."
        )

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

    if not cctx.spec.get("platform") and intent_field != "platform":
        missing.append(
            "platform - use the present_options tool (field \"platform\") to ask "
            "\"Which platform should we run this on?\" with chip choices: Google Ads, "
            "Meta. CALL the tool - never type the call into your reply."
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

    if (
        cctx.is_google
        and not cctx.competitor_analysis_attempted
        and "competitive_analysis_declined" not in cctx.spec
    ):
        # F11 · agentic, not a hardcoded phrase ladder: the MODEL interprets the
        # user's reply to THIS competitor offer (the old `lu in (...)` exact-match
        # missed "No, skip competitor analysis for now" → re-ask loop). Scoped +
        # biased to re-ask on doubt so a polarity-flip ("no, change the budget")
        # is never read as a decline. The _field_traceable guard backstops it.
        missing.append(
            "competitive analysis - offer it ONCE as a Yes/No question, then react:\n"
            "  • if you have not offered competitor analysis yet → ask via the "
            "present_options tool (field \"competitive_analysis_declined\"): \"Want me to "
            "analyze competitors before we set things up?\" with chips Yes / No. A No "
            "(or a clear typed decline) is recorded for you automatically - do NOT call "
            "set_campaign_spec for it, and never set a field the user hasn't stated "
            "(F17/F12: don't copy a value into duration/budget/account to 'proceed').\n"
            "  • they want it (yes / go ahead) → run analyze_competitors.\n"
            "  • the reply is unclear or about something ELSE (budget, a named competitor) "
            "→ re-ask the same Yes/No present_options; do NOT treat a doubtful reply as a "
            "decline.\n"
            "(These are instructions to CALL tools - never type tool-call syntax into your reply.)"
        )

    if not cctx.spec.get("duration"):
        if cctx.awaiting_custom_field == "duration":
            # v4 · F10 - user chose "Custom"; ask for a typed value, NOT chips.
            # The elicitation is kept open, so their typed reply is captured.
            missing.append(
                "duration - the user chose Custom. Ask them in one short line to "
                'TYPE the exact duration (e.g. "45 days", "6 weeks"). Do NOT call '
                "present_options or show chips again - their typed reply is captured "
                "automatically."
            )
        else:
            missing.append(
                "duration - use the present_options tool (field \"duration\") to ask "
                "\"How long should the campaign run?\" with chip choices: 30 days, "
                "60 days, 90 days, Custom. CALL the tool - never type the call into your reply."
            )
    if not cctx.spec.get("budget"):
        if cctx.awaiting_custom_field == "budget":
            # v4 · F10 - user chose "Custom"; ask for a typed value, NOT chips.
            currency = "₹" if cctx.is_real_estate else "$"
            missing.append(
                "budget - the user chose Custom. Ask them in one short line to TYPE "
                f'the exact daily budget (e.g. "{currency}7,500/day"). Do NOT call '
                "present_options or show chips again - their typed reply is captured "
                "automatically."
            )
        else:
            currency = "₹" if cctx.is_real_estate else "$"
            missing.append(
                "budget - use the present_options tool (field \"budget\") to ask "
                "\"What's your daily budget?\" with platform-tuned chip choices "
                f"(e.g. {currency}5,000/day, {currency}10,000/day, {currency}25,000/day) "
                "plus Custom. CALL the tool - never type the call into your reply."
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
        elif not cctx.spec.get("ig_page") and "ig_page_declined" not in cctx.spec:
            if is_ig_skip(cctx.last_user):
                missing.append(
                    "instagram - user is skipping Instagram (it's OPTIONAL). Call "
                    '`set_campaign_spec(ig_page_declined="true")` and proceed to review.'
                )
            elif cctx.ig_offered:
                # Already FETCHED - do NOT re-fetch (that was the live loop).
                # v5: fetch-time ≠ render-time. The marker is set when the fetch
                # tool returns, but the model may not have rendered the choice
                # yet - claiming "options are on screen" made it skip
                # present_options AND tell the user to click chips that didn't
                # exist. Prescribe the render instead of assuming it.
                missing.append(
                    "instagram - Instagram accounts were already fetched; do NOT call "
                    "fetch_meta_ig_accounts again. If you have NOT yet shown the choice, "
                    "call present_options EXACTLY as the fetch result instructed "
                    '(field="ig_page_declined"). If the user picked an account it\'s '
                    "captured. If they want Facebook only, call "
                    '`set_campaign_spec(ig_page_declined="true")`. If they\'re connecting '
                    "an Instagram account, wait and re-fetch only when they say they're ready."
                )
            else:
                missing.append(
                    "ig_page - Instagram is OPTIONAL. Call "
                    "`fetch_meta_ig_accounts(page_id=<stored fb_page>)`; the result tells you "
                    'the present_options call (it includes a "Continue with Facebook only" '
                    "option). If none are linked, the tool says so - offer Facebook-only."
                )

    if not missing and not cctx.keyword_research_done:
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
            "if competitive_analysis_declined='true'>\n\n"
            "EVERY bullet must be present - do not omit any.\n"
            "(2) THEN, separately, use the present_options tool to ask \"Proceed to build "
            "the campaign?\" with chips: Yes, proceed / No, make changes. When the user "
            "picks 'Yes, proceed', run the create_campaign tool (no arguments) - it "
            "researches the keywords and shows them in the review panel. These are tools "
            "to CALL - never type tool-call syntax into your reply, only the markdown "
            "summary above is text."
        )
    elif not missing:
        # keyword_research already done - keywords are in the panel; confirm launch.
        missing.append(
            "review keywords & launch - the keyword suggestions are shown in the panel. "
            "Ask the user to review and edit them (add / remove / edit), then call "
            "`present_options(question=\"Ready to launch the campaign?\", "
            "options=[\"Yes, launch\", \"No, make changes\"])`. "
            "**On the user's 'Yes, launch' reply, call `launch_campaign()` (no params) - "
            "that's the one tool that persists the campaign.**"
        )

    return missing
