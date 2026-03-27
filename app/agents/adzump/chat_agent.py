"""AdzumpChatAgent — the main conversational agent for ad campaign management.

Extends BaseAgent with:
- Campaign state tracking (phase, collected data, selected accounts)
- Progressive tool documentation by conversation phase
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.agent import BaseAgent
from app.core.session import BaseSession
from app.core.context import BaseContext
from app.agents.adzump.context import get_relevant_tool_details, build_adzump_context
from app.agents.adzump.tools.registry import ALL_TOOLS
from app.config import settings

logger = logging.getLogger(__name__)

# Campaign conversation phases
PHASE_INITIATED = "initiated"
PHASE_PLATFORM_SELECTION = "platform_selection"
PHASE_DATA_COLLECTION = "data_collection"
PHASE_ACCOUNT_SELECTION = "account_selection"
PHASE_CONFIRMATION = "confirmation"
PHASE_COMPLETED = "completed"
PHASE_OPTIMIZATION = "optimization"


class AdzumpChatAgent(BaseAgent):
    """Chat agent that manages ad campaigns through conversation.

    Tracks campaign state in session context and uses tools for
    website analysis, keyword research, and campaign management.

    Usage:
        agent = AdzumpChatAgent.get_instance()
        await agent.run(message, session, event_stream)
    """

    _instance: AdzumpChatAgent | None = None

    def __init__(self) -> None:
        context = build_adzump_context()
        context._cached_static_text = context._static_prefix

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
    def get_instance(cls) -> AdzumpChatAgent:
        """Get the singleton AdzumpChatAgent instance."""
        if cls._instance is None:
            cls._instance = cls()
            logger.info("AdzumpChatAgent created with %d tools", len(ALL_TOOLS))
        return cls._instance

    async def build_dynamic_context(self, session: BaseSession) -> str:
        """Build per-request dynamic context.

        Includes: auth info, campaign state summary, relevant tool docs.
        """
        parts: list[str] = []

        if session.auth:
            parts.append(
                f"Current session:\n"
                f"- Client: {session.auth.client_code}\n"
            )

        # Campaign state summary
        campaign_summary = self._build_campaign_summary(session)
        if campaign_summary:
            parts.append(campaign_summary)

        # Progressive tool docs based on conversation content
        tool_details = get_relevant_tool_details(session.messages)
        if tool_details:
            parts.append(tool_details)

        return "\n\n".join(parts)

    def _build_campaign_summary(self, session: BaseSession) -> str:
        """Build a detailed summary of current campaign state for the system prompt.

        This is critical — the LLM uses this to know what step it's on and
        what data has already been collected. Without this, it re-asks for info.
        """
        ctx = session.context
        phase = ctx.get("phase", PHASE_DATA_COLLECTION)

        parts = [f"## Current Campaign State\nCurrent Step: {phase}"]

        # Business info (from scrape_website tool)
        business = ctx.get("business_info")
        if business:
            parts.append(f"### Business Info (ALREADY COLLECTED — do NOT ask again)")
            parts.append(f"- Name: {business.get('brand_name', 'Unknown')}")
            parts.append(f"- Type: {business.get('business_type', 'Unknown')}")
            parts.append(f"- Location: {business.get('primary_location', 'Unknown')}")
            if business.get('service_areas'):
                parts.append(f"- Service Areas: {', '.join(business['service_areas'])}")
            if business.get('summary'):
                parts.append(f"- Summary: {business['summary'][:300]}")
            if business.get('unique_features'):
                parts.append(f"- USPs: {', '.join(business['unique_features'][:5])}")
            if business.get('products_services'):
                parts.append(f"- Products/Services: {', '.join(business['products_services'][:10])}")
        else:
            parts.append("### Business Info: NOT YET COLLECTED — ask for website URL or description")

        # Campaign data fields
        campaign = ctx.get("campaign_data", {})
        parts.append("\n### Campaign Fields")
        fields = {
            "platform": "Platform",
            "duration": "Campaign Duration",
            "budget": "Budget",
            "goal": "Advertising Goal (optional)",
            "leads_target": "Target Leads (optional)",
            "target_location": "Target Location",
        }
        optional_fields = {"goal", "leads_target"}
        for key, label in fields.items():
            value = campaign.get(key)
            if value:
                parts.append(f"- {label}: {value} (COLLECTED)")
            elif key in optional_fields:
                parts.append(f"- {label}: NOT COLLECTED (optional — skip if user declines)")
            else:
                parts.append(f"- {label}: NOT YET COLLECTED")

        # Selected accounts (platform-aware)
        platform = campaign.get("platform", "").lower()
        if platform == "meta":
            meta_account = ctx.get("meta_account")
            if meta_account:
                parts.append(f"\n### Meta Account: {meta_account.get('account_id', 'selected')} (SELECTED)")
            else:
                parts.append(f"\n### Meta Account: NOT YET SELECTED")
        elif platform == "google ads":
            google_account = ctx.get("google_account")
            if google_account:
                parts.append(f"\n### Google Ads Account: {google_account.get('customer_id', 'selected')} (SELECTED)")
            else:
                parts.append(f"\n### Google Ads Account: NOT YET SELECTED")
        else:
            parts.append(f"\n### Ad Account: Platform not yet selected")

        return "\n".join(parts) if len(parts) > 1 else ""

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        """Build context dict passed to each tool's execute function.

        Adds adzump-specific fields: session_context for campaign state.
        """
        ctx = super().build_tool_context(session)
        ctx["session_context"] = session.context
        return ctx
