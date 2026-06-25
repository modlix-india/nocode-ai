"""AdzumpAgent — conversational agent for ad-campaign construction.

Core design: keep the BaseAgent + tool loop, put every ounce of steering
into the **dynamic context**. Each turn renders:

1. ``## State`` — what's collected, with provenance ("just set" / "set N turns ago").
2. ``## User just said`` — last user message verbatim.
3. ``## What's still missing`` — ordered list from ``_next_action``.
4. ``## How to respond`` — 5-case priority rule for the LLM.

The static system prompt carries persona + non-negotiable rules only; the
workflow tree lives in Python (``_next_action``), computed from a typed
``CampaignContext`` view over ``session.context``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.core.agent import BaseAgent
from app.core.session import BaseSession
from app.agents.adzump.context import build_adzump_context
from app.agents.adzump.platform import (
    CANONICAL_LABEL,
    Platform,
    is_google as _platform_is_google,
    is_meta as _platform_is_meta,
)
from app.agents.adzump.tools.campaign_data import (
    _ACCOUNT_LIKE_FIELDS,
    _apply_field,
    _current_turn,
    _last_user_text,
    _normalize_id,
    is_clear_decline_reply,
    is_ig_skip,
    is_real_estate,
)
from app.agents.adzump.answer_parse import parse_typed_answer, currency_for
from app.agents.adzump.tools.registry import ALL_TOOLS
from app.agents.adzump.tools.suggestions import infer_suggestions
from app.config import settings

logger = logging.getLogger(__name__)



def _is_custom_reply(text: str) -> bool:
    """v4 · F10 — did the user pick the "Custom" escape on a chip ask? The chip's
    value is literally "Custom", so a click sends exactly that; a typed "custom
    amount" / "custom budget" also qualifies. Tight on purpose — presets are
    never "custom", so this won't fire on a real value."""
    lu = (text or "").strip().lower()
    return lu == "custom" or lu.startswith("custom")


def _hydrate_location_from_product_data(ctx: dict) -> None:
    """Restore location + mapped targets into campaign_spec/_location_meta from
    product_data on returning sessions, so the agent doesn't re-ask for a location
    that was already confirmed.

    Only runs when spec.location is unset but product_data.location exists. For
    local businesses, requires that geo-targets were already resolved (otherwise
    we must go through confirm_location again).
    """
    from app.agents.adzump.services.geo.discovery import is_local_business

    spec = ctx.setdefault("campaign_spec", {})
    if spec.get("location"):
        return
    product = ctx.get("product_data") or {}
    if not product.get("location"):
        return

    scale = (product.get("business_scale") or "national").lower().strip()
    has_resolved_targets = bool(product.get("google_mapped_locations")) or bool(
        product.get("meta_mapped_locations")
    )
    if is_local_business(scale) and not has_resolved_targets:
        return

    spec["location"] = product["location"]
    loc_meta = ctx.setdefault("_location_meta", {})
    loc_meta["address"] = product["location"]
    if product.get("product_coordinates"):
        coords = product["product_coordinates"]
        loc_meta["lat"] = coords.get("lat")
        loc_meta["lng"] = coords.get("lng")
    if "google_mapped_locations" in product:
        loc_meta["google_mapped_locations"] = product["google_mapped_locations"]
    if "meta_mapped_locations" in product:
        loc_meta["meta_mapped_locations"] = product["meta_mapped_locations"]
    logger.info(
        "hydrated_location_from_product_data: location=%s", product["location"]
    )


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
    # v3 · F3 — True once Instagram options have been offered (a multi-IG list
    # was shown, or the empty-IG Facebook-only choice). Stops _next_action from
    # re-prescribing the IG fetch every turn. Defaulted so existing test
    # fixtures that build CampaignContext directly need no change.
    ig_offered: bool = False
    # v4 · F10 — the field ("duration"/"budget") whose chip ask the user
    # escaped via "Custom"; we're now awaiting a typed value for it. Drives the
    # free-text prescription instead of re-rendering the same chips. Defaulted.
    awaiting_custom_field: str | None = None
    # True once create_campaign has run and stored keyword_research — flips the review
    # branch from "build the campaign" (run keyword research) to "launch". Defaulted so
    # existing test fixtures that build CampaignContext directly need no change.
    keyword_research_done: bool = False

    @classmethod
    def from_session(cls, session: BaseSession) -> "CampaignContext":
        ctx = session.context
        competitive_raw = ctx.get("competitor_analysis")
        competitive = competitive_raw or {}
        marker = ctx.get("_pending_location_confirm")
        # Marker may be a plain string (legacy) or dict (forward-compatible).
        if isinstance(marker, dict):
            pending_location = marker.get("location") or None
        elif isinstance(marker, str) and marker:
            pending_location = marker
        else:
            pending_location = None
        # v4 · F10 — the field whose chip ask was escaped via "Custom" (awaiting a
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
            # True iff `analyze_competitors` ran this session — even if it
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
        if self.is_google:
            locs = self.product.get("google_mapped_locations") or []
            return bool(locs)
        if self.is_meta:
            return bool(self.product.get("meta_mapped_locations"))
        return bool(self.product.get("target_areas"))


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
    call to make — including a suggested ``question`` argument for chip
    questions — so the LLM has nothing to construct, only to copy.
    """
    missing: list[str] = []

    if not cctx.product:
        missing.append("business URL — call `analyze_product(url=<the user's URL>)`")
        return missing

    # Intent routing: if the user's last message is a recognizable answer for
    # a pending field, surface "store this NOW" as the top of missing. This
    # prevents the LLM from following the default Next-action prescription
    # while ignoring the user's actual input. (E.g. user clicks "Google Ads"
    # chip while location is still missing — without this, the LLM would
    # call confirm_location and drop platform on the floor.)
    intent = _detect_intent(cctx)
    intent_field: str | None = None
    if intent is not None:
        intent_field, value = intent
        missing.append(
            f'{intent_field} — user said "{cctx.last_user[:40]}". '
            f"Call `set_campaign_spec({intent_field}={value!r})` FIRST."
        )

    if cctx.is_real_estate and not cctx.spec.get("location"):
        if cctx.pending_location:
            # Map shown last turn. Branch on user reply.
            detected = cctx.pending_location
            missing.append(
                f"location — map shown for **'{detected}'**. "
                f'If user said `"confirm"` → `set_campaign_spec(location="{detected}")`. '
                f'If JSON `{{"type":"location_update","address":"X",...}}` → '
                f'`set_campaign_spec(location="X")` (use address; fall back to '
                f'`"{detected}"`). '
                f"If user said WRONG/INCORRECT/NOT RIGHT → call `confirm_location()` again. "
                f"If user named a DIFFERENT city → `set_campaign_spec(location=<what they said>)`."
            )
        else:
            missing.append(
                "location — call `confirm_location()` (real estate business)"
            )

    if not cctx.spec.get("platform") and intent_field != "platform":
        missing.append(
            "platform — use the present_options tool (field \"platform\") to ask "
            "\"Which platform should we run this on?\" with chip choices: Google Ads, "
            "Meta. CALL the tool — never type the call into your reply."
        )

    has_platform = bool(cctx.spec.get("platform"))
    has_location = bool(cctx.spec.get("location"))
    # Coordinates restored from storage count as a valid anchor for discovery —
    # discover_geo_targets falls back to _location_meta lat/lng when no location
    # string is provided, so we can prescribe it without requiring a fresh confirm.
    _stored_coords = cctx.product.get("product_coordinates") or {}
    has_geo_anchor = has_location or bool(_stored_coords.get("lat"))
    if has_platform and has_geo_anchor and not cctx.has_mapped_geo_targets:
        loc_arg = cctx.spec.get("location") or ""
        missing.append(
            f"target_areas — call `discover_geo_targets({('location_name=' + repr(loc_arg)) if loc_arg else ''})`"
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
            "competitive analysis — offer it ONCE as a Yes/No question, then react:\n"
            "  • if you have not offered competitor analysis yet → ask via the "
            "present_options tool (field \"competitive_analysis_declined\"): \"Want me to "
            "analyze competitors before we set things up?\" with chips Yes / No. A No "
            "(or a clear typed decline) is recorded for you automatically — do NOT call "
            "set_campaign_spec for it, and never set a field the user hasn't stated "
            "(F17/F12: don't copy a value into duration/budget/account to 'proceed').\n"
            "  • they want it (yes / go ahead) → run analyze_competitors.\n"
            "  • the reply is unclear or about something ELSE (budget, a named competitor) "
            "→ re-ask the same Yes/No present_options; do NOT treat a doubtful reply as a "
            "decline.\n"
            "(These are instructions to CALL tools — never type tool-call syntax into your reply.)"
        )

    if not cctx.spec.get("duration"):
        if cctx.awaiting_custom_field == "duration":
            # v4 · F10 — user chose "Custom"; ask for a typed value, NOT chips.
            # The elicitation is kept open, so their typed reply is captured.
            missing.append(
                "duration — the user chose Custom. Ask them in one short line to "
                'TYPE the exact duration (e.g. "45 days", "6 weeks"). Do NOT call '
                "present_options or show chips again — their typed reply is captured "
                "automatically."
            )
        else:
            missing.append(
                "duration — use the present_options tool (field \"duration\") to ask "
                "\"How long should the campaign run?\" with chip choices: 30 days, "
                "60 days, 90 days, Custom. CALL the tool — never type the call into your reply."
            )
    if not cctx.spec.get("budget"):
        if cctx.awaiting_custom_field == "budget":
            # v4 · F10 — user chose "Custom"; ask for a typed value, NOT chips.
            currency = "₹" if cctx.is_real_estate else "$"
            missing.append(
                "budget — the user chose Custom. Ask them in one short line to TYPE "
                f'the exact daily budget (e.g. "{currency}7,500/day"). Do NOT call '
                "present_options or show chips again — their typed reply is captured "
                "automatically."
            )
        else:
            currency = "₹" if cctx.is_real_estate else "$"
            missing.append(
                "budget — use the present_options tool (field \"budget\") to ask "
                "\"What's your daily budget?\" with platform-tuned chip choices "
                f"(e.g. {currency}5,000/day, {currency}10,000/day, {currency}25,000/day) "
                "plus Custom. CALL the tool — never type the call into your reply."
            )
    # Account-block lines depend on the platform pick — skip until platform
    # is set so we don't suggest the wrong fetch tool.
    if cctx.spec.get("platform"):
        if not cctx.spec.get("parent_account"):
            fetch = (
                "fetch_google_parent_accounts"
                if cctx.is_google
                else "fetch_meta_parent_accounts"
            )
            missing.append(
                f"parent_account — call `{fetch}()` first; the result tells you "
                "the present_options call to make next."
            )
        if not cctx.spec.get("account"):
            fetch = "fetch_google_accounts" if cctx.is_google else "fetch_meta_accounts"
            missing.append(
                f"account — call `{fetch}(parent_id=<stored parent>)`; result tells you "
                "the present_options call."
            )

    if cctx.is_meta:
        if not cctx.spec.get("fb_page"):
            missing.append(
                "fb_page — call `fetch_meta_fb_pages(parent_id=<stored parent>)`; "
                "result tells you the present_options call."
            )
        # v3 · F3 — Instagram is OPTIONAL (Facebook-only is a valid campaign).
        # Offer it once; honour skip/later; never block. Gated on fb_page being
        # set so we ask one thing at a time.
        elif not cctx.spec.get("ig_page") and "ig_page_declined" not in cctx.spec:
            if is_ig_skip(cctx.last_user):
                missing.append(
                    "instagram — user is skipping Instagram (it's OPTIONAL). Call "
                    '`set_campaign_spec(ig_page_declined="true")` and proceed to review.'
                )
            elif cctx.ig_offered:
                # Already FETCHED — do NOT re-fetch (that was the live loop).
                # v5: fetch-time ≠ render-time. The marker is set when the fetch
                # tool returns, but the model may not have rendered the choice
                # yet — claiming "options are on screen" made it skip
                # present_options AND tell the user to click chips that didn't
                # exist. Prescribe the render instead of assuming it.
                missing.append(
                    "instagram — Instagram accounts were already fetched; do NOT call "
                    "fetch_meta_ig_accounts again. If you have NOT yet shown the choice, "
                    "call present_options EXACTLY as the fetch result instructed "
                    '(field="ig_page_declined"). If the user picked an account it\'s '
                    "captured. If they want Facebook only, call "
                    '`set_campaign_spec(ig_page_declined="true")`. If they\'re connecting '
                    "an Instagram account, wait and re-fetch only when they say they're ready."
                )
            else:
                missing.append(
                    "ig_page — Instagram is OPTIONAL. Call "
                    "`fetch_meta_ig_accounts(page_id=<stored fb_page>)`; the result tells you "
                    'the present_options call (it includes a "Continue with Facebook only" '
                    "option). If none are linked, the tool says so — offer Facebook-only."
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
            "review & build — TWO separate steps this turn:\n"
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
            "EVERY bullet must be present — do not omit any.\n"
            "(2) THEN, separately, use the present_options tool to ask \"Proceed to build "
            "the campaign?\" with chips: Yes, proceed / No, make changes. When the user "
            "picks 'Yes, proceed', run the create_campaign tool (no arguments) — it "
            "researches the keywords and shows them in the review panel. These are tools "
            "to CALL — never type tool-call syntax into your reply, only the markdown "
            "summary above is text."
        )
    elif not missing:
        # keyword_research already done — keywords are in the panel; confirm launch.
        missing.append(
            "review keywords & launch — the keyword suggestions are shown in the panel. "
            "Ask the user to review and edit them (add / remove / edit), then call "
            "`present_options(question=\"Ready to launch the campaign?\", "
            "options=[\"Yes, launch\", \"No, make changes\"])`. "
            "**On the user's 'Yes, launch' reply, call `launch_campaign()` (no params) — "
            "that's the one tool that persists the campaign.**"
        )

    return missing


class AdzumpAgent(BaseAgent):
    """Chat agent that manages ad campaigns through conversation."""

    _instance: "AdzumpAgent | None" = None

    # v9 I-1 · data-backed (two present_options stacked live 2026-05-30). When
    # the LLM batches a deferred elicitation with other tools, run them serially
    # and early-exit after the first elicitation so a second widget can't stack.
    force_serial_on_elicitation = True

    # ── construction ──

    def __init__(self) -> None:
        # _current_stream is stashed per-run so _on_loop_complete can emit
        # without needing event_stream in session.context (which is persisted
        # and can't hold live coroutine objects).
        self._current_stream = None
        context = build_adzump_context()
        provider = getattr(settings, "ADZUMP_PROVIDER", settings.LLM_PROVIDER)
        super().__init__(
            name="adzump",
            tools=ALL_TOOLS,
            context_builder=context,
            model_tier=settings.AGENT_MODEL_TIER,
            max_turns=settings.MAX_AGENT_TURNS,
            max_tokens=settings.AGENT_MAX_TOKENS,
            provider=provider,
        )

    @classmethod
    def get_instance(cls) -> "AdzumpAgent":
        if cls._instance is None:
            cls._instance = cls()
            logger.info("AdzumpAgent created with %d tools", len(ALL_TOOLS))
        return cls._instance

    # ── prompt-section builders (private) ──

    def _capture_tagged_answer(self, session: BaseSession, turn: int = 1) -> str:
        """PR2 · store the user's reply to a tagged ``present_options`` directly,
        before the LLM runs — so a forgotten ``set_campaign_spec`` follow-up
        can't drop the answer (the Bug-B family). Returns a one-line
        acknowledgement steer on a successful capture, else "".

        Gated to agentic ``turn == 1`` (the reply only just arrived) — NOT
        ``session._turn_count`` (restored to the max turn on resume). Match by
        exact option value (chips), else a conservative typed parser
        (duration/budget/platform). Ambiguous answers (Custom / "Yes" /
        off-topic / unparseable) leave ``_pending_elicitation`` intact and fall
        through to the LLM (then ``_resume_elicitation_section`` steers it)."""
        if turn != 1:
            return ""
        pe = session.context.get("_pending_elicitation")
        if not pe or not pe.get("field") or pe.get("expects") != "single":
            return ""
        field = pe["field"]
        answers = pe.get("answers") or {}
        last_user = _last_user_text({"_session": session})
        if not last_user:
            return ""
        value = answers.get(last_user)  # (a) exact chip match
        if value is None and field in ("duration", "budget", "platform"):
            value = parse_typed_answer(
                field, last_user, currency_for(session.context)
            )  # (b) typed
        if value is None and field == "competitive_analysis_declined" \
                and is_clear_decline_reply(last_user):
            value = "true"  # (c) F17 · typed clear decline
        if value is None:
            # v4 · F10 — the user picked the "Custom" escape on a duration/budget
            # chip ask. Don't pop the elicitation: keep it OPEN (mark it) so their
            # NEXT typed reply is captured by the typed-parser above, and steer a
            # free-text ask instead of re-rendering the same chips (the live loop,
            # bug #11). The mark also drives _next_action (awaiting_custom_field)
            # and tells _resume_elicitation_section not to pop.
            if (
                field in ("duration", "budget")
                and _is_custom_reply(last_user)
                and not pe.get("awaiting_custom")
            ):
                pe["awaiting_custom"] = True
                logger.info(
                    "tagged_capture: custom escape for field=%s — awaiting typed value",
                    field,
                )
                return (
                    "## The user chose a custom value\n"
                    f"They want to enter their own {field}. Ask them in ONE short line "
                    f'to TYPE it (e.g. "45 days" / "₹7,500/day") — do NOT call '
                    f"present_options or show chips. Their typed reply is captured automatically."
                )
            return ""  # Yes / off-topic / unparseable → LLM
        stored, info = _apply_field(
            field,
            value,
            last_user,
            session.context,
            _current_turn({"_session": session}),
        )
        if not stored:
            logger.info(
                "tagged_capture: rejected field=%s value=%r reason=%s user_said=%r",
                field,
                value,
                info,
                last_user[:80],
            )
            return ""
        session.context.pop("_pending_elicitation", None)
        # v3 · F4 — one-run marker: a tagged answer was captured this turn. Read
        # (and cleared) by get_pending_suggestions to suppress the untagged
        # infer_suggestions fallback when the LLM asks the next question as prose
        # (its chips would carry no field tag → silent drop → re-ask). Lives in
        # session.context but is popped on read, so it never leaks to next turn.
        session.context["_captured_this_turn"] = field
        logger.info(
            "tagged_capture: stored field=%s value=%r user_said=%r",
            field,
            value,
            last_user[:80],
        )
        return (
            "## You just captured the user's answer\n"
            f"Their last message set **{field} = {value}**. It is already stored — "
            "do NOT call set_campaign_spec for it. Acknowledge it in one short "
            "phrase, then CALL the next tool from the missing-list (a fetch tool or "
            "present_options) — do NOT write the next question as plain text, and "
            "NEVER end your turn without making that tool call (a live run stalled "
            "on a dead-end turn that acknowledged and stopped)."
        )

    def _record_prose_decline(
        self, session: BaseSession, cctx: "CampaignContext", last_user: str, turn: int,
    ) -> bool:
        """F18 · the competitor offer is non-deterministically asked as PROSE (no
        tagged ``present_options``), so a typed decline has no elicitation for
        ``_capture_tagged_answer`` to match — and the model often just advances
        without recording it, leaving ``competitive_analysis_declined`` unset and
        the prescription re-firing every turn. Record it in code at turn-start,
        with a NARROW guard (Kiran): only the competitor-offer state, only a
        clear-decline reply (``is_clear_decline_reply`` excludes ambiguous "no…"
        like "no competitors named yet" / "not now, first tell me about X"), and
        never when a competitor elicitation is already pending (tagged-capture
        owns that). Gated agentic ``turn == 1``. Returns True iff it stored."""
        if turn != 1 or not last_user:
            return False
        pe = session.context.get("_pending_elicitation")
        if pe and pe.get("field") == "competitive_analysis_declined":
            return False                                     # tagged-capture owns it
        if not (cctx.is_google
                and not cctx.competitor_analysis_attempted
                and "competitive_analysis_declined" not in cctx.spec):
            return False
        if not is_clear_decline_reply(last_user):
            return False                                     # ambiguous → let the LLM judge
        stored, _ = _apply_field(
            "competitive_analysis_declined", "true", last_user,
            session.context, _current_turn({"_session": session}),
        )
        if stored:
            logger.info("prose_decline_recorded: competitive_analysis_declined=true user_said=%r",
                        last_user[:80])
        return bool(stored)

    def _resume_elicitation_section(self, session: BaseSession, turn: int = 1) -> str:
        """v8 Plan B WS2 · when the previous turn ended on a deferred
        elicitation, tell the LLM it is resuming so it does NOT re-ask or
        paraphrase the question (Bug B). Read from session.context, which
        survives restore — message history drops tool blocks (session.py).

        Single-reply elicitations (location, options) are one-shot: emit the
        reminder, then clear the flag. Multi-reply elicitations (asset uploads)
        persist — the run loop closes them when the LLM moves on.

        Gated to agentic ``turn == 1``: the resume hint steers how the model
        reads the just-arrived reply, which only applies on the first turn of
        the run. On later turns return "" WITHOUT popping, so the one-shot flag
        survives (Approach B re-runs this builder every agentic turn — without
        the gate the pop would fire on turn 1 and the hint would vanish on
        turn 2+ of the SAME run, and the flag would be lost)."""
        if turn != 1:
            return ""
        pe = session.context.get("_pending_elicitation")
        if not pe:
            return ""
        if pe.get("expects") == "multi":
            return (
                "## Resuming — upload request is still open\n"
                "Last turn you asked the user to upload assets. They may send "
                "several messages (one per file) or say they're done. Do NOT "
                "restate the upload request unless they ask what's still needed, "
                "and do NOT assume it's closed until they signal completion or "
                "you judge the captured assets sufficient."
            )
        # v4 · F10 — awaiting a typed custom value: keep the elicitation OPEN
        # (do NOT pop) so the next typed reply is captured by the typed-parser.
        # _capture_tagged_answer already emitted the free-text steer this turn.
        if pe.get("awaiting_custom"):
            return ""
        # single: one-shot — clear after emitting so it fires for exactly this turn
        session.context.pop("_pending_elicitation", None)
        tool = pe.get("tool", "the previous step")
        return (
            "## Resuming after a question\n"
            f"Last turn you asked the user a question (via {tool}); the widget is "
            "already on screen. Their current message IS the reply. Do NOT restate "
            "or paraphrase the question, and do NOT call another tool with the "
            "previous tool's result as input — read their answer and pick the next action."
        )

    def _uploaded_assets_section(self, session: BaseSession) -> str:
        """v9 I-0 · when the user attached image(s) this turn, hand them to the
        Asset Manager via manage_assets (bytes are stashed on the session by the
        /chat handler). First action of the turn — otherwise the upload is lost
        (it only lives in the pending stash)."""
        pending = session.context.get("_pending_uploads")
        if not pending:
            return ""
        n = len(pending)
        return (
            f"## The user just uploaded {n} image{'s' if n != 1 else ''}\n"
            "FIRST, call `manage_assets` to hand the upload(s) to the Asset "
            "Manager — it looks at each image, decides what it is, and saves or "
            "skips it. You do NOT classify the image yourself; if the user said "
            "what it is, pass that as `note`. Do this before anything else, then "
            "continue."
        )

    def _state_section(self, cctx: CampaignContext) -> str:
        lines = ["## State"]

        if cctx.product:
            parts: list[str] = []
            if name := cctx.product.get("product_name"):
                parts.append(name)
            if bt := cctx.product.get("business_type"):
                parts.append(f"({bt})")
            lines.append(f"- Product: {' '.join(parts) or '(unnamed)'}")
        else:
            lines.append("- Product: — (need URL)")

        # Surface the analyzed URL so the review summary can include it
        # without the LLM hunting for it across nested structures.
        url = (
            cctx.product_profile.get("url")
            or (cctx.product.get("pages_analyzed") or [None])[0]
            or ""
        )
        if url:
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
            or "competitive_analysis_declined" in cctx.spec
        ):
            lines.append("- Competitors: none analyzed")

        for key, label in (
            ("location", "Location"),
            ("platform", "Platform"),
            ("duration", "Duration"),
            ("budget", "Budget"),
        ):
            val = cctx.spec.get(key)
            prov = self._provenance(key, cctx.set_at, cctx.current_turn)
            if val:
                lines.append(f"- {label}: {val} ✓{prov}")
            else:
                lines.append(f"- {label}: —")

        target_areas = cctx.product.get("target_areas") or []
        if target_areas:
            area_names = [a.get("name") for a in target_areas if a.get("name")]
            lines.append(f"- Target Areas: {', '.join(area_names)} ✓")

        account_block = self._ad_account_summary(cctx.spec, cctx.account_names)
        if account_block.strip():
            lines.append(account_block.rstrip())

        return "\n".join(lines)

    # ── static leaf helpers — migrations ──

    @staticmethod
    def _migrate_legacy_keys(ctx: dict) -> None:
        """Rename ``campaign_data`` → ``campaign_spec`` for pre-rename sessions.

        Lazy migration. O(1). Existing sessions survive the rename transparently.
        """
        if "campaign_data" in ctx and "campaign_spec" not in ctx:
            ctx["campaign_spec"] = ctx.pop("campaign_data")

    @staticmethod
    def _migrate_campaign_ids(session_ctx: dict) -> None:
        """Canonicalize account/page ids (strip dashes/whitespace) on read.

        Lazy migration for sessions that stored dashed or fullwidth-digit IDs
        before the write-side normalizer shipped. Idempotent.
        """
        spec = session_ctx.get("campaign_spec") or {}
        for field_name in _ACCOUNT_LIKE_FIELDS:
            v = spec.get(field_name)
            if isinstance(v, str):
                canonical = _normalize_id(v)
                if canonical != v:
                    spec[field_name] = canonical

    # ── static leaf helpers — prompt formatters ──

    @staticmethod
    def _provenance(field_name: str, set_at: dict, current_turn: int) -> str:
        if field_name not in set_at:
            return ""
        turn = int(set_at[field_name])
        delta = max(0, current_turn - turn)
        if delta == 0:
            return " — just set"
        if delta == 1:
            return " — set 1 turn ago"
        return f" — set {delta} turns ago"

    @staticmethod
    def _user_said_section(last_user: str) -> str:
        if not last_user:
            return "\n## User just said\n(no user message yet)"
        preview = last_user.replace("\n", " ")
        if len(preview) > 500:
            preview = preview[:500] + "…"
        return f'\n## User just said\n"{preview}"'

    @staticmethod
    def _missing_section(missing: list[str]) -> str:
        if not missing:
            return "\n## What's still missing\n(nothing — ready for review & publish)"
        # Render each pending item with its full prescription. Top-1 is
        # marked as the immediate next action; the rest let the LLM keep
        # going within the same agentic-loop turn (e.g. after storing
        # platform, call confirm_location for location).
        lines = [
            "\n## What's still missing (in order — do the top item first)",
            "Example values below (e.g. \"30 days\", \"₹5,000/day\") are OPTIONS to "
            "SHOW the user via present_options — NEVER values to store. Only "
            "`set_campaign_spec` a field after the user actually states it (F12).",
        ]
        for i, item in enumerate(missing, 1):
            lines.append(f"{i}. {item}")
        return "\n".join(lines)

    @staticmethod
    def _how_to_respond_section() -> str:
        return (
            "\n## How to respond (first match wins)\n"
            "1. Info question → answer briefly from State, then do the Next action.\n"
            "2. Correction → `set_campaign_spec(<field>=<new>)`, acknowledge, then re-check Next action.\n"
            "3. **New data** (typed or chip-clicked) → `set_campaign_spec(<field>=<value>)` IMMEDIATELY, "
            "even if the value is for a different field than Next action. "
            'Examples: user says "Google Ads" → `set_campaign_spec(platform="Google Ads")`. '
            'User says "₹10,000/day" → `set_campaign_spec(budget="₹10,000/day")`. '
            "Then acknowledge in one short sentence and re-check Next action.\n"
            '4. Ambient ("ok", "continue", "next") → just do Next action.\n'
            "5. Otherwise → do Next action.\n"
            "\n**A tool already spoke?** When a tool posts its own result to the "
            "user (assets saved/skipped/corrected, competitors added/skipped — these "
            "now appear in chat automatically), do NOT repeat it; write only a short "
            "one-line lead-in to the Next action.\n"
            "\n**One ask per turn.** Never call two question-asking tools "
            "(`confirm_location`, `present_options`) in the same turn — ask one, "
            "wait for the reply, then ask the next. (The runtime also enforces "
            "this, but don't rely on it.)\n"
            "\n**Tool syntax is INTERNAL — never print it.** The `tool(question=…, "
            "options=[…], field=…)` forms in '## What's still missing' are "
            "instructions for YOU to CALL — never text to show the user. CALL the "
            "tool; your visible reply is natural prose only. NEVER write a tool "
            "name or `tool(...)` call syntax into the chat."
        )

    @staticmethod
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

        def pretty_id(acct_id: str) -> str:
            raw = str(acct_id)
            if is_google_platform and raw.isdigit() and len(raw) == 10:
                return f"{raw[:3]}-{raw[3:6]}-{raw[6:]}"
            return raw

        def fmt(acct_id: str | None) -> str:
            if not acct_id:
                return "—"
            name = (account_names.get(str(acct_id)) or "").strip()
            display_id = pretty_id(acct_id)
            return f"{name} (ID: {display_id})" if name else f"ID: {display_id}"

        lines = [
            f"- {parent_label}: {fmt(spec.get('parent_account'))}",
            f"- {account_label}: {fmt(spec.get('account'))}",
        ]
        if is_meta_platform:
            lines.append(f"- Facebook Page: {fmt(spec.get('fb_page'))}")
            lines.append(f"- Instagram Account: {fmt(spec.get('ig_page'))}")
        return "\n".join(lines)

    # ── public surface — BaseAgent override hooks (last, per Kiran's BaseAgent) ──

    async def run(self, user_message, session, event_stream, image_blocks=None, model_override=None):
        """Stash event_stream so _on_loop_complete can emit without session.context."""
        self._current_stream = event_stream
        try:
            await super().run(user_message, session, event_stream, image_blocks, model_override)
        finally:
            self._current_stream = None

    async def build_dynamic_context(self, session: BaseSession) -> str:
        return (
            ""  # adzump context is fully per-turn — see build_turn_reminder (Layer 2)
        )

    async def build_turn_reminder(self, session: BaseSession, turn: int) -> str:
        self._migrate_legacy_keys(session.context)
        self._migrate_campaign_ids(session.context)
        # PR2 · capture the user's tagged answer into campaign_spec BEFORE the
        # snapshot, so the just-answered field drops out of the missing-list
        # this turn. AFTER the migrations (its setdefault would otherwise strand
        # a legacy rename); gated to agentic turn==1 internally.
        ack = self._capture_tagged_answer(session, turn)

        _hydrate_location_from_product_data(session.context)
        cctx = CampaignContext.from_session(session)
        last_user = _last_user_text({"_session": session})
        # F18 · when the competitor offer was asked as PROSE (not a tagged
        # present_options), a clear typed decline has no capture rail — record it
        # in code at turn-start so competitive_analysis_declined doesn't persist
        # in `missing` forever. Re-derive cctx since the spec changed.
        if self._record_prose_decline(session, cctx, last_user, turn):
            cctx = CampaignContext.from_session(session)
        missing = _next_action(cctx)
        logger.info(
            "next_action: turn=%d agentic=%d missing=%s user_said=%r",
            cctx.current_turn,
            turn,
            missing,
            last_user[:80],
        )
        reminder = "\n".join(
            filter(
                None,
                [
                    ack,
                    self._uploaded_assets_section(session),
                    self._resume_elicitation_section(session, turn),
                    self._state_section(cctx),
                    self._user_said_section(last_user),
                    self._missing_section(missing),
                    self._how_to_respond_section(),
                ],
            )
        )
        return reminder

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        ctx = super().build_tool_context(session)
        ctx["session_context"] = session.context
        ctx["_session"] = session
        # Use the full session_id — the previous `[:8]` truncation left only
        # one hex char of entropy after the `SYSTEM_` prefix, so distinct
        # sessions routinely collided onto the same craft_id and stomped
        # each other's UI panel state.
        session.context.setdefault("craft_id", f"adzump_{session.session_id}")
        if session.auth:
            ctx["auth"] = session.auth
        return ctx

    async def _on_loop_complete(
        self, session: BaseSession, tool_call_log: list[dict[str, Any]],
    ) -> None:
        await super()._on_loop_complete(session, tool_call_log)
        ctx = session.context
        from app.agents.adzump.services.business_storage import save_campaign, resolve_url
        if resolve_url(ctx):
            try:
                await save_campaign(ctx, self.build_tool_context(session))
            except Exception as e:
                logger.debug("End-of-turn campaign save failed (non-fatal): %s", e)

        # If platform was set this turn but existing locations aren't yet mapped
        # for it (storage-reuse path: locations existed before platform was chosen),
        # map them now so has_mapped_geo_targets is true for the next turn's prompt.
        # This runs at end-of-turn where external I/O belongs — NOT in build_turn_reminder.
        spec = ctx.get("campaign_spec") or {}
        platform_eot = spec.get("platform") or ""
        product_eot = ctx.get("product_data") or {}
        target_areas_eot = product_eot.get("target_areas") or []
        if platform_eot and target_areas_eot:
            from app.agents.adzump.platform import is_google as _ig, is_meta as _im
            from app.agents.adzump.services.geo.mapping import PlatformGeoMapper
            loc_meta_eot = ctx.setdefault("_location_meta", {})
            cc_eot = loc_meta_eot.get("country_code") or "IN"
            needs_google = _ig(platform_eot) and not product_eot.get("google_mapped_locations")
            needs_meta = _im(platform_eot) and not product_eot.get("meta_mapped_locations")
            if needs_google or needs_meta:
                try:
                    tool_ctx = self.build_tool_context(session)
                    mapped_eot = await PlatformGeoMapper(ctx, tool_ctx).map_target_areas(
                        target_areas_eot, platform_eot, cc_eot
                    )
                    if mapped_eot:
                        product_eot["target_areas"] = mapped_eot
                        if needs_google:
                            product_eot["google_mapped_locations"] = mapped_eot
                            loc_meta_eot["google_mapped_locations"] = mapped_eot
                        else:
                            product_eot["meta_mapped_locations"] = mapped_eot
                            loc_meta_eot["meta_mapped_locations"] = mapped_eot
                except Exception as e:
                    logger.warning("End-of-turn geo auto-mapping failed (non-fatal): %s", e)

        # If platform was just set this turn and mapped locations already exist
        # (storage reuse path), emit the craft panel with platform so the map
        # appears — discover_geo_targets won't fire because has_mapped_geo_targets
        # is already True.
        cctx = CampaignContext.from_session(session)
        platform = cctx.spec.get("platform") or ""
        if platform and cctx.has_mapped_geo_targets and ctx.get("_last_craft_platform") != platform:
            ctx["_last_craft_platform"] = platform
            url = resolve_url(ctx)
            product = ctx.get("product_data") or {}
            stream = self._current_stream
            craft_id = ctx.get("craft_id") or ctx.get("_craft_id")
            if stream and craft_id and url:
                from app.agents.adzump.tools.craft import emit_craft_panel
                from app.agents.adzump.platform import is_google as _is_google, is_meta as _is_meta
                try:
                    await emit_craft_panel(
                        stream, craft_id, url, product,
                        ctx.get("competitor_analysis") or {},
                        screenshot_url=(
                            product.get("primary_screenshot_url")
                            or product.get("screenshot_url")
                        ),
                        baked_summary=(
                            (ctx.get("product_profile") or {}).get("summary")
                            or product.get("summary", "")
                        ),
                        platform=platform,
                    )
                except Exception as e:
                    logger.debug("Post-platform craft emit failed (non-fatal): %s", e)

                # Emit a visible locations row so the user sees the stored
                # targeting areas being applied (mirrors discover_geo_targets UX).
                try:
                    if _is_google(platform):
                        mapped = product.get("google_mapped_locations") or []
                    elif _is_meta(platform):
                        mapped = product.get("meta_mapped_locations") or []
                    else:
                        mapped = product.get("target_areas") or []
                    if mapped:
                        loc_meta = ctx.get("_location_meta") or {}
                        await stream.emit_data(
                            "suggested_locations",
                            {
                                "locations": [loc["name"] for loc in mapped if loc.get("name")],
                                "targeting_type": product.get("business_scale", "local"),
                                "location": loc_meta.get("address") or "",
                                "from_storage": True,
                            },
                        )
                except Exception as e:
                    logger.debug("Stored-locations row emit failed (non-fatal): %s", e)

    @staticmethod
    def _advance_chip(text: str) -> dict[str, Any] | None:
        """F23 · a prose advance/confirm ask (e.g. "Let's confirm the location for
        the campaign") with no live widget gets ONE value-only quick-reply chip so
        the user can click instead of having to type to proceed. Value-only (no
        ``field``/``answer``) → the click just sends "yes …" text that routes to the
        model, so it CANNOT reintroduce the F4 untagged-capture loop. Keyword-gated
        + deterministic (no extra LLM call), per the panel. Returns the chip dict or
        None."""
        lt = (text or "").lower()
        if not lt:
            return None
        # F27 · evaluate the TRAILING line (the actual ask), NOT the whole blob —
        # a launch/review summary lists a "Location:" bullet and contains "ready
        # to" (in "Ready to launch"), which made this fire a "Confirm location"
        # chip at the launch step. The genuine advance ask is always its own short
        # trailing line, so anchoring there fixes the over-match.
        tail = next((ln for ln in reversed(lt.splitlines()) if ln.strip()), "")
        markers = (
            "let's confirm", "lets confirm", "confirm the location", "shall i",
            "shall we", "ready to", "ready when you", "go ahead", "look good",
            "looks good", "proceed", "all set",
        )
        if not any(m in tail for m in markers):
            return None
        # Label precedence: launch → location → generic (launch must win — the
        # launch ask's trailing line is "…Ready to launch the campaign?").
        if "launch" in tail:                                   # F27 · launch step
            return {"options": [{"label": "Yes, launch",
                                 "value": "yes, launch"}], "mode": "single"}
        if "location" in tail:
            return {"options": [{"label": "Confirm location",
                                 "value": "yes, confirm the location"}], "mode": "single"}
        return {"options": [{"label": "Go ahead", "value": "yes, go ahead"}], "mode": "single"}

    async def get_pending_suggestions(
        self,
        session: BaseSession,
        assistant_text: str = "",
    ) -> dict[str, Any] | None:
        # v3 · F4 — pop the one-run capture marker FIRST, before any early
        # return, so it can never leak into the next turn (it rides the
        # persisted context dict, which the generic run-loop doesn't clear).
        # Kept here (not in core/agent.py) so the Adzump-specific marker stays
        # out of the generic runtime.
        captured = session.context.pop("_captured_this_turn", None)
        pending = session.context.pop("_pending_suggestions", None)
        if pending:
            return pending
        # When a map widget is in flight, the widget IS the answer mechanism —
        # don't let the inferrer auto-inject competing Yes/No chips.
        if session.context.get("_pending_location_confirm"):
            return None
        # v8 Plan B WS6 · any deferred elicitation already owns the ask (map,
        # upload prompt, explicit chips). Suppress the untraced infer_suggestions
        # fallback so it can't overlay competing chips on the elicitation bubble
        # (and to skip its extra per-turn gpt-4o-mini call). Full tracing-wrap
        # of infer_suggestions remains a fast-follow.
        if session.context.get("_pending_elicitation"):
            return None
        # v3 · F4 — a tagged answer was captured this turn, but no tagged
        # present_options was emitted (we'd have returned above). The LLM asked
        # the next question as prose → suppress the untagged infer fallback whose
        # chips would carry no field tag (a click wouldn't be captured → re-ask
        # loop). The user can type the answer (typed-capture handles
        # duration/budget/platform); next turn the LLM re-asks via a tagged tool.
        # F23 · a prose advance/confirm ask (no live widget) gets one value-only
        # "Go ahead"/"Confirm location" chip so the user needn't type to proceed.
        # BEFORE the captured-guard so it fires even right after a chip answer (the
        # dead-end case). Value-only → can't reintroduce the F4 untagged-capture loop.
        adv = AdzumpAgent._advance_chip(assistant_text)
        if adv:
            return adv
        if captured:
            return None
        return await infer_suggestions(assistant_text, session.context)
