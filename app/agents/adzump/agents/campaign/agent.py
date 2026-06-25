"""CampaignAgent — platform-agnostic campaign-creation orchestrator.

A BaseAgent (same shape as the optimization agent) spawned by the main adzump
agent's create_campaign tool once the user confirms the campaign summary. It runs
the selected platform's creation tools; each tool calls the relevant sub-agent
(keyword_research -> the keyword agent). For now: Google Search -> keyword_research.
More tools and campaign types slot in without changing this shell.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.core.agent import BaseAgent
from app.core.session import AuthContext, BaseSession
from app.core.streaming import AgentEventStream

from app.agents.adzump.agents.campaign.context import build_campaign_context
from app.agents.adzump.agents.campaign.tools.google.keyword_research import GOOGLE_CAMPAIGN_TOOLS

logger = logging.getLogger(__name__)

PROVIDER = "openai"
MODEL_TIER = "balanced"
MAX_TURNS = 5  # call the platform tool(s) and stop — small loop
MAX_TOKENS = 2000
_LOG_TRUNCATE = 200


class _CampaignStream(AgentEventStream):
    """Forwards the panel + sub-agent lifecycle events to the parent stream, and
    swallows the orchestrator's own prose + the terminating done/feedback (the parent
    — the main agent — owns those for the SSE stream)."""

    def __init__(self, parent: AgentEventStream) -> None:
        self._parent = parent  # deliberately no super().__init__()

    @property
    def is_cancelled(self) -> bool:
        return getattr(self._parent, "is_cancelled", False)

    def cancel(self) -> None:
        self._parent.cancel()

    async def emit_data(self, *a, **kw):
        await self._parent.emit_data(*a, **kw)

    async def emit_craft(self, *a, **kw):
        await self._parent.emit_craft(*a, **kw)

    async def emit_craft_text(self, *a, **kw):
        await self._parent.emit_craft_text(*a, **kw)

    async def emit_tool_start(self, *a, **kw):
        await self._parent.emit_tool_start(*a, **kw)

    async def emit_tool_update(self, *a, **kw):
        await self._parent.emit_tool_update(*a, **kw)

    async def emit_tool_result(self, *a, **kw):
        await self._parent.emit_tool_result(*a, **kw)

    async def emit_agent_started(self, *a, **kw):
        await self._parent.emit_agent_started(*a, **kw)

    async def emit_agent_finished(self, *a, **kw):
        await self._parent.emit_agent_finished(*a, **kw)

    async def emit_agent_usage(self, *a, **kw):
        await self._parent.emit_agent_usage(*a, **kw)

    async def emit_text(self, text: str) -> None:
        return

    async def emit_thinking(self, reasoning: str) -> None:
        return

    async def emit_error(self, message: str) -> None:
        logger.debug("campaign substream error: %s", message[:_LOG_TRUNCATE])

    async def emit_done(self, *a, **kw) -> None:
        return

    async def emit_feedback_request(self, *a, **kw) -> None:
        return

    async def emit_suggestions(self, *a, **kw) -> None:
        return

    async def emit_keepalive(self) -> None:
        return


class CampaignAgent(BaseAgent):
    """Deterministic-shell campaign orchestrator (one platform's tools at a time)."""

    display_name = "Campaign Creation"
    _instance: "CampaignAgent | None" = None

    def __init__(self) -> None:
        context = build_campaign_context()
        context._cached_static_text = context._static_prefix  # no async docs to load
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
    def get_instance(cls) -> "CampaignAgent":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def build_dynamic_context(self, session: BaseSession) -> str:
        spec = session.context.get("campaign_spec") or {}
        return f"Platform: {spec.get('platform', '')} · Channel: SEARCH (keywords apply)."

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        ctx = super().build_tool_context(session)
        ctx["session_context"] = session.context  # tools read campaign_spec / product_data here
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
    ) -> dict | None:
        """Run campaign creation and return the keyword_research result (or None).

        Spawned by the main agent's create_campaign tool. Seeds a sub-session with the
        collected campaign data; the platform tools read it and write their output
        (keyword_research) back into that session, which we return for the caller to
        persist on the main session.
        """
        session = BaseSession(agent_name="campaign")
        await session.get_or_create(None, auth)
        session.context = {
            "campaign_spec": campaign_spec,
            "product_data": product_data,
            "craft_id": craft_id,
        }
        run_start = time.monotonic()
        try:
            await self.run(
                user_message="Create the campaign.",
                session=session,
                event_stream=_CampaignStream(parent_event_stream),
            )
        except Exception as exc:
            await self._emit_finished(parent_event_stream, run_start, session, "error", type(exc).__name__)
            raise

        result = session.context.get("keyword_research")
        await self._emit_finished(parent_event_stream, run_start, session, "success", "keyword research complete")
        return result

    @staticmethod
    async def _emit_finished(
        parent: AgentEventStream, run_start: float, session: BaseSession, status: str, summary: str,
    ) -> None:
        try:
            usage = session.total_usage or {}
            await parent.emit_agent_finished(
                agent_id="campaign", status=status,
                duration_ms=int((time.monotonic() - run_start) * 1000),
                tokens_in=int(usage.get("input_tokens") or 0),
                tokens_out=int(usage.get("output_tokens") or 0),
                summary=summary,
            )
        except Exception:
            pass


def get_campaign_agent() -> CampaignAgent:
    return CampaignAgent.get_instance()
