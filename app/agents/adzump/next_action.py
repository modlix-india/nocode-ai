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
    # Image sessions generated via manage_creatives → used to avoid re-generating
    # on every turn and to inject edit context into the prescription.
    # Defaulted so existing test fixtures that build CampaignContext directly
    # need no change.
    image_sessions: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        # Provide mutable default without triggering dataclass field default issues.
        if self.image_sessions is None:
            object.__setattr__(self, "image_sessions", {})

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
            image_sessions=dict(ctx.get("_image_sessions") or {}),
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


def _handle_creative_workflow(cctx: CampaignContext) -> list[str] | None:
    """Creative approval/modification sub-states.

    Returns a missing list if creatives need attention, None otherwise.
    Extracted from _next_action for readability.
    """
    if not cctx.spec.get("ad_copy") or cctx.spec.get("creative_approved") == "true":
        return None

    missing: list[str] = []

    if cctx.spec.get("creative_approved") in ["false", "edit_copy_styling"]:
        missing.append(
            "creative changes - ask the user what changes they would like to make. "
            "Once they provide their feedback, call `manage_creatives(user_message=<the user's full request>)`."
        )
    elif cctx.spec.get("creative_approved") == "generate_new_sizes":
        if not cctx.spec.get("creative_target_sizes"):
            missing.append(
                "ask target sizes - ask the user which sizes they would like to generate. "
                'Call `present_options(question="Which sizes would you like to generate?", '
                "options=["
                '{"label":"Portrait (9:16)","value":"portrait","answer":"portrait"}, '
                '{"label":"Landscape (16:9)","value":"landscape","answer":"landscape"}, '
                '{"label":"Square (1:1)","value":"square","answer":"square"}, '
                '{"label":"All sizes","value":"square,portrait,landscape","answer":"square,portrait,landscape"}'
                '], field="creative_target_sizes")`'
            )
        else:
            target_sizes = cctx.spec.get("creative_target_sizes")
            missing.append(
                f"generate new sizes - user wants to generate sizes: {target_sizes}. Call "
                "`set_campaign_spec(creative_approved=None, creative_target_sizes=None)` "
                "to reset the approval, then call "
                f"`manage_creatives(user_message='generate {target_sizes} sizes')`."
            )
    elif cctx.spec.get("creative_approved") == "generate_competitor_inspired":
        missing.append(
            "generate competitor-inspired creative - user wants a competitor-inspired creative. Call "
            "`set_campaign_spec(creative_approved=None, creative_config='2')` "
            "to reset the approval, then call `manage_creatives(user_message='generate competitor-inspired creative')`."
        )
    elif cctx.spec.get("creative_approved") == "generate_persona_variations":
        if not cctx.spec.get("creative_target_personas"):
            missing.append(
                "ask target personas - ask the user which target personas or demographics they want to focus on. "
                'Call `present_options(question="Which target personas or demographics do you want to focus on for the ad campaign?", '
                "options=["
                '{"label":"First-time Homebuyers","value":"First-time Homebuyers","answer":"First-time Homebuyers"}, '
                '{"label":"Young Families","value":"Young Families","answer":"Young Families"}, '
                '{"label":"Investors","value":"Investors","answer":"Investors"}, '
                '{"label":"Retirees","value":"Retirees","answer":"Retirees"}, '
                '{"label":"Professionals","value":"Professionals","answer":"Professionals"}, '
                '{"label":"Custom","value":"Custom","answer":"Custom"}'
                '], field="creative_target_personas")`'
            )
        else:
            target_personas = cctx.spec.get("creative_target_personas")
            if target_personas.lower().strip() == "custom":
                missing.append(
                    "custom target personas - the user chose Custom. Ask the user in one short line to TYPE "
                    "the target personas or demographics they want to target. Once they provide the feedback, "
                    "call `set_campaign_spec(creative_target_personas=<what they typed>)`."
                )
            else:
                missing.append(
                    f"generate persona variations - user wants to generate variations for personas: {target_personas}. Call "
                    "`set_campaign_spec(creative_approved=None, creative_target_personas=None)` "
                    "to reset the approval, then call "
                    f"`manage_creatives(user_message='generate for personas: {target_personas}')`."
                )
    else:
        creative_previews = ""
        ad_copy_list = cctx.spec.get("ad_copy") or []
        if isinstance(ad_copy_list, list):
            for idx, item in enumerate(ad_copy_list, 1):
                urls = item.get("creative_urls", {})
                for size_name, url in urls.items():
                    if url:
                        creative_previews += f'\n![{size_name.capitalize()}]({url}){{style="width: 250px; height: 250px; object-fit: contain; border-radius: 8px; margin: 4px;"}}'

        missing.append(
            "creative approval - You MUST output the following markdown ad creative previews in your chat response so the user can see them:\n"
            f"{creative_previews}\n\n"
            "After showing the previews, call "
            '`present_options(question="Would you like to proceed with these creatives or make choices?", '
            'options=[{"label":"Proceed","value":"true","answer":"true"}, '
            '{"label":"Edit copy/styling","value":"edit_copy_styling","answer":"edit_copy_styling"}, '
            '{"label":"Generate new sizes","value":"generate_new_sizes","answer":"generate_new_sizes"}, '
            '{"label":"Generate competitor-inspired creative","value":"generate_competitor_inspired","answer":"generate_competitor_inspired"}, '
            '{"label":"Generate persona variations","value":"generate_persona_variations","answer":"generate_persona_variations"}], field="creative_approved")`'
        )

    return missing


def _is_approval_or_ambient(text: str) -> bool:
    """Check if the user's message is a clear approval or an ambient reply.

    Used by the creative workflow to distinguish between:
    - Approvals → show previews and ask via present_options (formal approval)
    - Edit requests → route directly to `manage_creatives` without re-asking.

    The invert-approval approach treats anything that is NOT a clear approval
    or ambient response as an edit intent. This is intentional: the cost of a
    false positive (routing an accidental question to manage_creatives) is low
    — the CreativeAgent LLM handles it gracefully — while a false negative
    (ignoring the user's edit request) breaks the flow entirely.
    """
    if not text:
        return True
    t = text.strip().lower().rstrip(".!?,")

    # Clear approvals (chip answer values + common typed variants)
    if t in {
        "true",
        "yes",
        "yeah",
        "yep",
        "yup",
        "sure",
        "looks great",
        "looks good",
        "great",
        "good",
        "perfect",
        "nice",
        "awesome",
        "amazing",
        "love it",
        "approved",
        "approve",
        "proceed",
        "looks perfect",
        "that's great",
        "that looks great",
        "that looks good",
        "yes, looks great",
        "yes, looks great!",
    }:
        return True

    # Ambient responses (the user isn't engaging with the ask)
    if t in {"ok", "okay", "continue", "next", "go ahead", "sure go ahead"}:
        return True

    return False


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

    creative_missing = _handle_creative_workflow(cctx)
    if creative_missing is not None:
        missing.extend(creative_missing)
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
            'platform - use the present_options tool (field "platform") to ask '
            '"Which platform should we run this on?" with chip choices: Google Ads, '
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
            'present_options tool (field "competitive_analysis_declined"): "Want me to '
            'analyze competitors before we set things up?" with chips Yes / No. A No '
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
                'duration - use the present_options tool (field "duration") to ask '
                '"How long should the campaign run?" with chip choices: 30 days, '
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
                'budget - use the present_options tool (field "budget") to ask '
                '"What\'s your daily budget?" with platform-tuned chip choices '
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

    if not missing:
        assets = cctx.product.get("assets") or {}
        has_logo = any(logo.get("url") for logo in assets.get("logos") or [])
        if not has_logo:
            missing.append(
                "brand logo - brand logo is missing. Ask the user in one short phrase to upload their brand logo. "
                "Do NOT proceed until a logo is uploaded via `manage_assets`."
            )
        elif not cctx.image_sessions:
            # No images generated yet — tell the agent to ask the user for their
            # preferred format BEFORE calling manage_creatives, so every campaign
            # gets the right size instead of always defaulting to 1:1 square.
            missing.append(
                "ad creative generation - Ask the user which format they want "
                "(Square 1:1, Portrait 9:16, Landscape 16:9, or Story 4:5), then call "
                "`manage_creatives(user_message=<the user's full creative request "
                "including format>)`."
            )
        elif any(
            info.get("status") == "generating" for info in cctx.image_sessions.values()
        ):
            # Generation still in flight — do not add any prescription; the
            # agent loop will re-evaluate once the tool result comes back.
            pass
        elif cctx.spec.get("creative_approved") == "true":
            # User explicitly approved the creatives — proceed to review & publish.
            meta_extra = ""
            if cctx.is_meta:
                meta_extra = "\n  - **Facebook Page**: <copy verbatim from State, including '(ID: …)'>"
                meta_extra += (
                    "\n  - **Instagram Account**: <copy verbatim from State, including '(ID: …)'>"
                    if cctx.spec.get("ig_page")
                    else "\n  - **Instagram Account**: not linked (Facebook only)"
                )
            # Build creative preview from image_sessions
            creative_previews = ""
            for img_id, info in cctx.image_sessions.items():
                url = info.get("current_image_url")
                if url and info.get("status") == "done":
                    creative_previews += (
                        f'\n<img src="{url}" alt="{img_id}" '
                        'style="width:250px;height:250px;object-fit:contain;border-radius:8px;margin:4px;" />'
                    )
            # Also include any ad_copy-based creatives
            ad_copy_list = cctx.spec.get("ad_copy") or []
            if isinstance(ad_copy_list, list):
                for idx, item in enumerate(ad_copy_list, 1):
                    urls = item.get("creative_urls", {})
                    for size_name, url in urls.items():
                        if url:
                            creative_previews += (
                                f'\n<img src="{url}" alt="{size_name.capitalize()}" '
                                'style="width:250px;height:250px;object-fit:contain;border-radius:8px;margin:4px;" />'
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
                f"{meta_extra}"
                f"{creative_previews}\n"
                "  - **Competitors**: <comma-separated names from State, or 'none analyzed' "
                "if competitor_analysis_attempted is true with empty list, or 'declined' "
                "if competitive_analysis_declined='true'>\n\n"
                "EVERY bullet must be present - do not omit any.\n"
                '(2) THEN, separately, use the present_options tool to ask "Ready to launch '
                'the campaign?" with chips: Yes, launch / No, make changes. When the user '
                "picks 'Yes, launch', run the launch_campaign tool (no arguments) - the one "
                "tool that persists the campaign. These are tools to CALL - never type "
                "tool-call syntax into your reply, only the markdown summary above is text."
            )
        elif cctx.spec.get("creative_approved") == "edit":
            # User clicked "Edit / make changes" on a previous turn.
            # Two sub-states based on whether they've provided feedback yet.
            last = (cctx.last_user or "").strip().lower().rstrip(".!?,")
            if last in ("edit", "edit / make changes"):
                # (A) They just clicked the chip — no edit instruction yet.
                missing.append(
                    "ad creative edit - The user chose to edit. Ask them in ONE "
                    "short sentence what they would like to change. Once they "
                    "provide feedback, call "
                    "`manage_creatives(user_message=<their full edit instruction>)`, "
                    "then call "
                    "`set_campaign_spec(creative_approved=null)` so the updated "
                    "image can be re-shown for approval."
                )
            else:
                # (B) User typed their edit instruction — route to manage_creatives.
                missing.append(
                    "ad creative edit - Call "
                    "`manage_creatives(user_message=<their verbatim edit instruction>)`. "
                    "The creative subsystem will identify which image to edit. "
                    "After the edit completes, call "
                    "`set_campaign_spec(creative_approved=null)` so the updated "
                    "image can be re-shown for approval."
                )
        else:
            # ── CATCH-ALL: images exist, done, and not yet approved ──
            # Covers creative_approved=None (first time — never shown previews)
            # and any unknown value. Always shows previews; does NOT attempt
            # invert-approval here because a non-approval message might be
            # about something else entirely (e.g. "change the website url").
            # The edit chip path is handled by the explicit "edit" branch above.
            img_previews = ""
            for img_id, info in cctx.image_sessions.items():
                url = info.get("current_image_url")
                ratio = info.get("aspect_ratio", "1:1")
                if url and info.get("status") == "done":
                    img_previews += (
                        f'\n<img src="{url}" alt="{img_id} ({ratio})" '
                        'style="max-width:300px;border-radius:8px;margin:4px;" />'
                    )
            image_ids_str = ", ".join(
                f"{k} ({v.get('aspect_ratio', '?')})"
                for k, v in cctx.image_sessions.items()
                if v.get("status") == "done"
            )
            missing.append(
                "ad creative review - You MUST output the following markdown image "
                "previews in your chat response so the user can see the generated creatives:\n"
                f"{img_previews}\n\n"
                "After showing the previews, call "
                '`present_options(question="Do these creatives meet your expectations?", '
                'options=[{"label":"Yes, looks great!","value":"true","answer":"true"}, '
                '{"label":"Edit / make changes","value":"edit","answer":"edit"}], '
                'field="creative_approved")`\n'
                f"Existing image IDs for editing: {image_ids_str}. "
                "When the user chooses Edit and provides feedback, call "
                "`manage_creatives(user_message=<edit instruction>, "
                "image_id=<the image ID to edit>)` and then reset creative_approved by calling "
                "`set_campaign_spec(creative_approved=null)` so the updated image can be re-shown."
            )

    return missing
