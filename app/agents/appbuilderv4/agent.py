"""AppBuilderV4Agent — minimal agent driving the code_run sandbox.

Differences from v3 (`AppBuilderAgent`):
- ONE tool (`code_run`) by default. No HOT_TOOLS, no deferred-schema dance.
- Persona ≈ 60 lines vs ~600 in v3. Discovery happens via the SDK at runtime.
- No CONFIRMATION_TOOLS — the subprocess boundary is the safety net.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings
from app.core.agent import BaseAgent
from app.core.context import BaseContext
from app.core.session import BaseSession

logger = logging.getLogger(__name__)


class AppBuilderV4Agent(BaseAgent):
    """Code-first builder agent. Add tools to this class only when a bench
    scenario fails for lack of one. Track every addition in CLAUDE.md."""

    def __init__(
        self,
        context_builder: BaseContext,
        tools: list | None = None,
        provider: str = "anthropic",
    ) -> None:
        super().__init__(
            name="appbuilderv4",
            tools=tools or [],
            context_builder=context_builder,
            model_tier=settings.AGENT_MODEL_TIER,
            # Hard cap on turns. Most v4 tasks converge in 3-8 code_run
            # calls; clone tasks need WAY more headroom — extract assets +
            # multiple regions + multiple compare rounds per region. We
            # let it grow to settings.MAX_AGENT_TURNS (default ~100). The
            # SDK still catches runaway loops via per-tool subprocess
            # timeouts and the agent's own 5-call-per-task discipline.
            max_turns=settings.MAX_AGENT_TURNS,
            max_tokens=settings.AGENT_MAX_TOKENS,
            provider=provider,
            # Full schemas for the (tiny) tool set ship in the system prompt;
            # no deferred-schema indirection needed when there are 1-3 tools.
            defer_schemas=False,
        )

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        """Pass app/client codes + the live session context dict into the
        tool context. `session_context` is the shared dict that lets tools
        cache state across calls in the same conversation (e.g. screenshot
        handles for compare_to_source to look up later)."""
        ctx = super().build_tool_context(session)
        if session.auth:
            ctx["app_code"] = session.context.get("app_code") or session.auth.app_code
            ctx["client_code"] = session.auth.client_code
        ctx["session_context"] = session.context
        return ctx

    async def build_dynamic_context(self, session: BaseSession) -> str:
        """Tiny per-turn context: just the session identity. Everything else
        the agent discovers via `code_run` + the SDK."""
        if not session.auth:
            return ""
        ac = session.context.get("app_code") or session.auth.app_code
        cc = session.auth.client_code
        return (
            f"Session identity:\n"
            f"- Client: {cc}\n"
            f"- App:    {ac}\n"
            f"`modlix.config` inside code_run is pre-bound to these.\n"
        )
