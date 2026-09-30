"""CampaignAgent — platform-agnostic campaign-creation orchestrator.

A BaseAgent (same shape as the optimization agent) spawned by the main adzump
agent's prepare_campaign_review tool once the user confirms the campaign summary. It runs
the selected platform's creation tools; each tool calls the relevant sub-agent
(keyword_research -> the keyword agent). For now: Google Search -> keyword_research.
More tools and campaign types slot in without changing this shell.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.agents.adzump.agents._child_stream import ChildAgentStream
from app.agents.adzump.agents.campaign.context import build_campaign_context
from app.agents.adzump.agents.campaign.models import (
    Channel,
    build_dump,
    resolve_channel,
    set_build,
)
from app.agents.adzump.agents.campaign.tools.google.registry import (
    GOOGLE_CAMPAIGN_TOOLS,
)
from app.config import settings
from app.core.agent import BaseAgent
from app.core.session import AuthContext, BaseSession
from app.core.streaming import AgentEventStream

logger = logging.getLogger(__name__)

PROVIDER = "deepseek"  # matches the keyword agent it spawns
MODEL_TIER = "balanced"
MAX_TURNS = 5  # call the platform tool(s) and stop — small loop
# A reasoning model spends output tokens deliberating before it calls a tool, so the budget
# covers both.
MAX_TOKENS = settings.AGENT_MAX_TOKENS


class _CampaignStream(ChildAgentStream):
    """Forwards panel + sub-agent lifecycle (including its own tool calls) to the
    parent; the base swallows the orchestrator's prose and the parent-owned terminators."""

    label = "campaign"


class CampaignAgent(BaseAgent):
    """Deterministic-shell campaign orchestrator (one platform's tools at a time)."""

    display_name = "Campaign Creation"
    _instance: CampaignAgent | None = None

    def __init__(self) -> None:
        context = build_campaign_context()
        context.use_static_prefix_only()  # no async docs to load
        super().__init__(
            name="campaign",
            tools=GOOGLE_CAMPAIGN_TOOLS,
            context_builder=context,
            model_tier=MODEL_TIER,
            max_turns=MAX_TURNS,
            max_tokens=MAX_TOKENS,
            provider=PROVIDER,
        )

    @classmethod
    def get_instance(cls) -> CampaignAgent:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def build_dynamic_context(self, session: BaseSession) -> str:
        spec = session.context.get("campaign_spec") or {}
        channel = resolve_channel(spec)
        note = (
            "keywords apply"
            if channel is Channel.SEARCH
            else "audience-targeted, no keywords"
        )
        return (
            f"Platform: {spec.get('platform', '')} · Channel: {channel.value} ({note})."
        )

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        ctx = super().build_tool_context(session)
        ctx["session_context"] = (
            session.context
        )  # tools read campaign_spec / product_data here
        if session.auth:
            ctx["auth"] = session.auth
        return ctx

    async def create(
        self,
        *,
        campaign_spec: dict,
        product_data: dict,
        craft_id: str,
        parent_event_stream: AgentEventStream,
        auth: AuthContext,
        build: dict | None = None,
    ) -> dict | None:
        """Run campaign creation and return the build it produced (or None).

        Spawned by the main agent's prepare_campaign_review tool. Seeds a throwaway
        sub-session with the collected campaign data; the platform tools read it and write
        their output back into that session, which we hand to the caller to keep on the
        main session. The whole build rather than one channel's slot - this shell does not
        know which channel's tool ran.

        ``build`` is what the main session already holds. Without it every tool starts blind:
        their "already built for these inputs" checks read the session they run in, so on a
        retry after a partial failure they cannot tell finished work from missing work and
        redo all of it.
        """
        session = BaseSession(agent_name="campaign")
        await session.get_or_create(None, auth)
        session.context = {
            "campaign_spec": campaign_spec,
            "product_data": product_data,
            "craft_id": craft_id,
        }
        if build:
            set_build(session.context, build)
        run_start = time.monotonic()
        stream = _CampaignStream(parent_event_stream)
        try:
            await self.run(
                user_message="Create the campaign.",
                session=session,
                event_stream=stream,
            )
        except Exception as exc:
            await stream._emit_finished(
                agent_id="campaign",
                run_start=run_start,
                session=session,
                status="error",
                summary=type(exc).__name__,
            )
            raise

        result = build_dump(session.context)
        await stream._emit_finished(
            agent_id="campaign",
            run_start=run_start,
            session=session,
            status="success",
            summary="campaign build complete",
        )
        return result


def get_campaign_agent() -> CampaignAgent:
    return CampaignAgent.get_instance()
