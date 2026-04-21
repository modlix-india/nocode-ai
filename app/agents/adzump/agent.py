"""AdzumpAgent — the main conversational agent for ad campaign management.

Extends BaseAgent with:
- Campaign state tracking via session.context
- Dynamic campaign summary showing collected vs missing data
- All tools always available — LLM guided by campaign summary
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.agent import BaseAgent
from app.core.session import BaseSession
from app.agents.adzump.context import build_adzump_context
from app.agents.adzump.tools.registry import ALL_TOOLS
from app.agents.adzump.tools.suggestions import infer_suggestions
from app.config import settings

logger = logging.getLogger(__name__)


class AdzumpAgent(BaseAgent):
    """Chat agent that manages ad campaigns through conversation.

    All tools are always available to the LLM. The campaign summary
    (what's collected vs missing) guides the LLM on what to do next.

    Usage:
        agent = AdzumpAgent.get_instance()
        await agent.run(message, session, event_stream)
    """

    _instance: AdzumpAgent | None = None

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
    def get_instance(cls) -> AdzumpAgent:
        """Get the singleton AdzumpAgent instance."""
        if cls._instance is None:
            cls._instance = cls()
            logger.info("AdzumpAgent created with %d tools", len(ALL_TOOLS))
        return cls._instance

    # Campaign fields: (context_key, optional?)
    _CAMPAIGN_FIELDS = [
        ("platform", False),
        ("duration", False),
        ("budget", False),
    ]

    # Business info fields: (context_key, is_list?, max_items/chars)
    # summary is kept FULL (no truncation) — it's the primary product context
    # the LLM uses in every downstream turn (ad copy, platform picks, etc.).
    _BUSINESS_FIELDS = [
        ("product_name", False, None),
        ("business_type", False, None),
        ("summary", False, None),
        ("unique_features", True, 5),
        ("products_services", True, 10),
    ]

    async def build_dynamic_context(self, session: BaseSession) -> str:
        """Build campaign state summary showing collected vs missing data."""
        ctx = session.context
        parts = [
            "## Current Campaign State",
            self._product_data_summary(ctx.get("product_data")),
            self._campaign_data_summary(ctx.get("campaign_data", {})),
        ]
        return "\n".join(parts)

    def _product_data_summary(self, business: dict | None) -> str:
        if not business:
            return "### Product Data: NOT YET COLLECTED — ask for website URL or description"

        lines = ["### Product Data (ALREADY COLLECTED — do NOT ask again)"]
        for key, is_list, limit in self._BUSINESS_FIELDS:
            label = key.replace("_", " ").title()
            value = business.get(key)
            if not value:
                continue
            if is_list:
                lines.append(f"- {label}: {', '.join(value[:limit] if limit else value)}")
            elif limit:
                lines.append(f"- {label}: {value[:limit]}")
            else:
                lines.append(f"- {label}: {value}")

        loc = business.get("location", {})
        if isinstance(loc, str):
            if loc:
                lines.append(f"- Location: {loc}")
        elif isinstance(loc, dict):
            if loc.get("location"):
                lines.append(f"- Location: {loc['location']}")
            if loc.get("suggested_locations"):
                lines.append(f"- Suggested Ad Locations: {', '.join(loc['suggested_locations'])}")

        return "\n".join(lines)

    def _campaign_data_summary(self, campaign: dict) -> str:
        lines = ["\n### Campaign Fields"]
        for key, optional in self._CAMPAIGN_FIELDS:
            label = key.replace("_", " ").title()
            value = campaign.get(key)
            if value:
                lines.append(f"- {label}: {value} (COLLECTED)")
            elif optional:
                lines.append(f"- {label}: NOT COLLECTED (optional)")
            else:
                lines.append(f"- {label}: NOT YET COLLECTED")

        lines.append(self._ad_account_summary(campaign))

        return "\n".join(lines)

    @staticmethod
    def _ad_account_summary(campaign: dict) -> str:
        platform = campaign.get("platform", "").lower()
        account_map = {"google": ("google_account", "Google Ads Account"), "meta": ("meta_account", "Meta Account")}
        for plat, (acct_key, label) in account_map.items():
            if plat in platform:
                acct = campaign.get(acct_key)
                return f"\n### {label}: {acct} (SELECTED)" if acct else f"\n### {label}: NOT YET SELECTED"
        return "\n### Ad Account: Platform not yet selected"

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        """Build context dict passed to each tool's execute function."""
        ctx = super().build_tool_context(session)
        ctx["session_context"] = session.context
        ctx["_session"] = session  # direct ref so tools can ensure writes persist
        # Stable craft panel ID for this session — all tools read this instead
        # of computing their own, so product + competitor panels always match.
        session.context.setdefault("craft_id", f"adzump_{session.session_id[:8]}")
        # Expose the raw auth object — needed by tools that spawn sub-sessions
        # (e.g. analyze_business → ProductAgent).
        if session.auth:
            ctx["auth"] = session.auth
        return ctx

    async def get_pending_suggestions(
        self, session: BaseSession, assistant_text: str = "",
    ) -> dict[str, Any] | None:
        """Check for pending suggestions, with text-based fallback.

        First checks if the LLM called present_options (stored in context).
        If not, scans the assistant text for common choice patterns.
        """
        # Primary: LLM called present_options. Fallback: infer from text
        # using the session context so suggestions are tailored to the
        # business/campaign (e.g. lead-target presets that match price tier).
        pending = session.context.pop("_pending_suggestions", None)
        return pending or await infer_suggestions(assistant_text, session.context)
