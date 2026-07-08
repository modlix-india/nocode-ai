"""Adzump2Agent — conversational builder over the CampaignPlan IR.

P0 skeleton. Unlike the legacy adzump agent (which mirrors the campaign
spec in session context and computes a Python workflow tree per turn),
adzump2 keeps the plan on the adzump Java service: tools merge-patch it
there, and the per-turn reminder is rendered from the last completeness
snapshot the plan tools stashed into session context (the "completeness
rail").
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.adzump2.context import build_adzump2_context
from app.agents.adzump2.tools.registry import ALL_TOOLS
from app.config import settings
from app.core.agent import BaseAgent
from app.core.session import BaseSession

logger = logging.getLogger(__name__)


class Adzump2Agent(BaseAgent):
    """Chat agent that builds ad campaigns as a server-side CampaignPlan."""

    _instance: "Adzump2Agent | None" = None

    # TODO(P1): add launch_campaign (and any other money-moving tools) here
    # when launch lands, so they pause for explicit user confirmation.
    CONFIRMATION_TOOLS: set[str] = set()

    # ── construction ──

    def __init__(self) -> None:
        super().__init__(
            name="adzump2",
            tools=ALL_TOOLS,
            context_builder=build_adzump2_context(),
            model_tier=settings.AGENT_MODEL_TIER,
            max_turns=settings.MAX_AGENT_TURNS,
            provider=getattr(settings, "ADZUMP2_PROVIDER", settings.LLM_PROVIDER),
        )

    @classmethod
    def get_instance(cls) -> "Adzump2Agent":
        if cls._instance is None:
            cls._instance = cls()
            logger.info("Adzump2Agent created with %d tools", len(ALL_TOOLS))
        return cls._instance

    # ── BaseAgent override hooks ──

    async def build_turn_reminder(self, session: BaseSession, turn: int) -> str:
        """The completeness rail (P0): steer toward the next missing slot.

        Rendered from the last completeness snapshot stashed by the plan
        tools (``session.context["plan_completeness"]``) — no I/O here.
        """
        ctx = session.context
        completeness = ctx.get("plan_completeness")

        if completeness is None:
            if not ctx.get("plan_id"):
                return (
                    "## Plan status\n"
                    "No CampaignPlan exists yet. Get the campaign name (and product, "
                    "if known) from the user, then call create_plan before anything else."
                )
            return (
                "## Plan status\n"
                "A plan exists but its completeness is unknown — call "
                "get_completeness to see which required slots are missing."
            )

        if completeness.get("complete"):
            return (
                "## Plan status\n"
                "All required slots are filled. Run validate_plan, then review "
                "the plan with the user."
            )

        missing = completeness.get("missingRequired") or []
        lines = ["## Plan status — required slots still missing"]
        lines.extend(f"- {slot}" for slot in missing)
        lines.append(
            "Ask the user for ONE next slot (advisory — if they volunteer "
            "something else, capture that instead)."
        )
        lines.append("Edit the plan ONLY via update_plan (merge patch).")
        lines.append(
            "Never invent platform ids — ids come from fetcher tools or the user."
        )
        return "\n".join(lines)

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        ctx = super().build_tool_context(session)
        ctx["session_context"] = session.context
        ctx["_session"] = session
        if session.auth:
            ctx["auth"] = session.auth
        return ctx
