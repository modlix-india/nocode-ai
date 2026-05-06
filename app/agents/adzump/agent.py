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
    _ACCOUNT_LIKE_FIELDS, _last_user_text, _normalize_id,
)
from app.agents.adzump.tools.registry import ALL_TOOLS
from app.agents.adzump.tools.suggestions import infer_suggestions
from app.config import settings

logger = logging.getLogger(__name__)


# Substrings in ``product_data.business_type`` that flag a session as
# real-estate. Matches the scraper's metadata prompt.
_REAL_ESTATE_KEYWORDS = (
    "real estate", "realty", "villa", "apartment", "residential",
    "property", "housing", "homes", "realtor", "township", "builder", "developer",
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
        return cls(
            product=ctx.get("product_data") or {},
            product_profile=ctx.get("product_profile") or {},
            competitor_names=[
                c.get("name") for c in (competitive.get("competitors") or [])
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
        )

    @property
    def is_real_estate(self) -> bool:
        bt = (self.product.get("business_type") or "").lower()
        return any(kw in bt for kw in _REAL_ESTATE_KEYWORDS)

    @property
    def is_google(self) -> bool:
        return _platform_is_google(self.spec.get("platform"))

    @property
    def is_meta(self) -> bool:
        return _platform_is_meta(self.spec.get("platform"))


def _detect_intent(cctx: CampaignContext) -> tuple[str, str] | None:
    """Recognize when the user's last message is an obvious answer for a
    pending campaign-spec field. Returns (field, value) to store, or None.

    Conservative: only matches unambiguous cases. Anything subtle (custom
    durations, free-form budgets) is left to the LLM via the default
    missing-list prescription.
    """
    lu = (cctx.last_user or "").strip().lower()
    if not lu:
        return None
    spec = cctx.spec

    # Platform: "Google Ads" / "Meta" chip clicks or close natural-language
    # variants. Only fire if platform isn't already stored. Defers keyword
    # classification to app.agents.adzump.platform so all consumers stay
    # aligned on which strings count as which platform.
    if not spec.get("platform"):
        platform = Platform.from_value(lu)
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
            f"{intent_field} — user said \"{cctx.last_user[:40]}\". "
            f"Call `set_campaign_spec({intent_field}={value!r})` FIRST."
        )

    if cctx.is_real_estate and not cctx.spec.get("location"):
        if cctx.pending_location:
            # Map shown last turn. Branch on the user's reply.
            detected = cctx.pending_location
            missing.append(
                f"location — map shown for **'{detected}'**. "
                f"If user said `\"confirm\"` → `set_campaign_spec(location=\"{detected}\")`. "
                f"If JSON `{{\"type\":\"location_update\",\"address\":\"X\",...}}` → "
                f"`set_campaign_spec(location=\"X\")` (use address; fall back to "
                f"`\"{detected}\"`). "
                f"If user said WRONG/INCORRECT/NOT RIGHT → call `confirm_location()` again. "
                f"If user named a DIFFERENT city → `set_campaign_spec(location=<what they said>)`."
            )
        else:
            missing.append("location — call `confirm_location()` (real-estate)")

    if not cctx.spec.get("platform") and intent_field != "platform":
        missing.append(
            "platform — call `present_options(question=\"Which platform should we run this on?\", "
            "options=[\"Google Ads\", \"Meta\"])`"
        )

    if (cctx.is_google
            and not cctx.competitor_analysis_attempted
            and "competitive_analysis_declined" not in cctx.spec):
        # Pending-aware: if user just said no/skip, drop the question and
        # store the declined flag. If user said yes (or anything else), the
        # LLM follows the prescription.
        lu = cctx.last_user.strip().lower()
        if lu in ("no", "n", "no thanks", "skip", "no need"):
            missing.append(
                "competitive analysis — user said NO. Call "
                "`set_campaign_spec(competitive_analysis_declined=\"true\")` and proceed."
            )
        else:
            missing.append(
                "competitive analysis — call `present_options(question=\"Want me to analyze "
                "competitors before we set things up?\", options=[\"Yes\", \"No\"])`. "
                "If the user just said YES, call `analyze_competitors()` instead."
            )

    if not cctx.spec.get("duration"):
        missing.append(
            "duration — call `present_options(question=\"How long should the campaign run?\", "
            "options=[\"30 days\", \"60 days\", \"90 days\", \"Custom\"])`"
        )
    if not cctx.spec.get("budget"):
        currency = "₹" if cctx.is_real_estate else "$"
        missing.append(
            "budget — call `present_options(question=\"What's your daily budget?\", "
            f"options=[<platform-tuned presets, e.g. {currency}5,000/day, {currency}10,000/day, "
            f"{currency}25,000/day>, \"Custom\"])`"
        )
    # Account-block lines depend on the platform pick — skip until platform
    # is set so we don't suggest the wrong fetch tool.
    if cctx.spec.get("platform"):
        if not cctx.spec.get("parent_account"):
            fetch = "fetch_google_parent_accounts" if cctx.is_google else "fetch_meta_parent_accounts"
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
        if not cctx.spec.get("ig_page"):
            missing.append(
                "ig_page — call `fetch_meta_ig_accounts(page_id=<stored fb_page>)`; "
                "result tells you the present_options call."
            )

    if not missing:
        meta_extra = (
            "\n  - **Facebook Page**: <copy verbatim from State, including '(ID: …)'>"
            "\n  - **Instagram Account**: <copy verbatim from State, including '(ID: …)'>"
            if cctx.is_meta else ""
        )
        missing.append(
            "review & publish — your reply this turn is EXACTLY this markdown, "
            "with values copied VERBATIM from the `## State` block above (do NOT "
            "rephrase, do NOT drop fields, do NOT replace IDs with placeholders "
            "like 'Linked' or 'Connected', do NOT abbreviate):\n\n"
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
            "Then call `present_options(question=\"Ready to launch the campaign?\", "
            "options=[\"Yes, launch\", \"No, make changes\"])`. EVERY bullet must be "
            "present — do not omit any. "
            "**On the user's 'Yes, launch' reply, call `launch_campaign()` "
            "(no params) — that's the one tool that persists the campaign.**"
        )

    return missing


class AdzumpAgent(BaseAgent):
    """Chat agent that manages ad campaigns through conversation."""

    _instance: "AdzumpAgent | None" = None

    def __init__(self) -> None:
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

    # ── Dynamic context (called every turn, placed AFTER the static prefix
    # so Anthropic's cache still hits the stable system prompt) ──────────

    async def build_dynamic_context(self, session: BaseSession) -> str:
        self._migrate_legacy_keys(session.context)
        self._migrate_campaign_ids(session.context)
        cctx = CampaignContext.from_session(session)
        last_user = _last_user_text({"_session": session})
        missing = _next_action(cctx)
        logger.info(
            "next_action: turn=%d missing=%s user_said=%r",
            cctx.current_turn, missing, last_user[:80],
        )
        return "\n".join([
            self._state_section(cctx),
            self._user_said_section(last_user),
            self._missing_section(missing),
            self._how_to_respond_section(),
        ])

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

    # ── Dynamic context sections ─────────────────────────────────────────

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
        url = (cctx.product_profile.get("url")
               or (cctx.product.get("pages_analyzed") or [None])[0]
               or "")
        if url:
            lines.append(f"- Website: {url}")

        if cctx.competitor_names:
            names = ", ".join(cctx.competitor_names[:5])
            suffix = f" (+{len(cctx.competitor_names) - 5} more)" if len(cctx.competitor_names) > 5 else ""
            lines.append(f"- Competitors: {names}{suffix} ✓")
        elif cctx.competitor_analysis_attempted or "competitive_analysis_declined" in cctx.spec:
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

        account_block = self._ad_account_summary(cctx.spec, cctx.account_names)
        if account_block.strip():
            lines.append(account_block.rstrip())

        return "\n".join(lines)

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
        lines = ["\n## What's still missing (in order — do the top item first)"]
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
            "Examples: user says \"Google Ads\" → `set_campaign_spec(platform=\"Google Ads\")`. "
            "User says \"₹10,000/day\" → `set_campaign_spec(budget=\"₹10,000/day\")`. "
            "Then acknowledge in one short sentence and re-check Next action.\n"
            "4. Ambient (\"ok\", \"continue\", \"next\") → just do Next action.\n"
            "5. Otherwise → do Next action."
        )

    @staticmethod
    def _ad_account_summary(spec: dict, account_names: dict) -> str:
        platform = Platform.from_value(spec.get("platform"))
        if platform is None:
            return ""
        is_meta_platform = platform is Platform.META
        is_google_platform = platform is Platform.GOOGLE
        parent_label = (
            "Meta Business" if is_meta_platform
            else "Google Manager" if is_google_platform
            else "Parent Account"
        )
        account_label = (
            "Meta Ad Account" if is_meta_platform
            else "Google Ad Account" if is_google_platform
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

    # ── BaseAgent hooks ──────────────────────────────────────────────────

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        ctx = super().build_tool_context(session)
        ctx["session_context"] = session.context
        ctx["_session"] = session
        session.context.setdefault("craft_id", f"adzump_{session.session_id[:8]}")
        if session.auth:
            ctx["auth"] = session.auth
        return ctx

    async def get_pending_suggestions(
        self, session: BaseSession, assistant_text: str = "",
    ) -> dict[str, Any] | None:
        pending = session.context.pop("_pending_suggestions", None)
        if pending:
            return pending
        # When a map widget is in flight, the widget IS the answer mechanism —
        # don't let the inferrer auto-inject competing Yes/No chips.
        if session.context.get("_pending_location_confirm"):
            return None
        return await infer_suggestions(assistant_text, session.context)
