"""LocationAgent - the one agent that owns campaign geo-targeting.

``handle()`` (behind the ``manage_targeting_locations`` tool) runs the agent's
own tool-use loop; the model acts by picking one of the four location tools.
Step helpers live in ``targeting_run.py``; every mutation ends in
``tools._shared.finalize_targets``. Design rationale: AGENT.md.
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Any

from app.core.agent import BaseAgent
from app.core.session import BaseSession
from app.core.streaming import AgentEventStream, pre_emit_agent_started
from app.core.tools.base import ToolResult
from app.agents.adzump.agents.location.context import build_location_context
from app.agents.adzump.agents.location.subagent_event_stream import (
    LocationPassthroughEventStream,
)
from app.agents.adzump._shared import build_ds_headers
from app.agents.adzump.agents.location.targeting_run import (
    build_run_prompt,
    build_run_result,
    build_sub_session,
    resolve_coordinates,
    resolve_country_geo_constant,
    resolve_location_name,
    validate_run_context,
)
from app.agents.adzump.agents.location.tools import LOCATION_AGENT_TOOLS

logger = logging.getLogger(__name__)

LOCATION_PROVIDER = "deepseek"
LOCATION_MODEL_TIER = "fast"
LOCATION_MAX_TURNS = 10  # 1 reasoning + 1 tool + 1 summary + slack
LOCATION_MAX_TOKENS = 4096


class LocationAgent(BaseAgent):
    """One tool-use agent for geo-targeting: discovery and manual edits."""

    display_name = "Location Agent"

    def __init__(self) -> None:
        super().__init__(
            name="location_agent",
            tools=LOCATION_AGENT_TOOLS,
            context_builder=build_location_context(),
            model_tier=LOCATION_MODEL_TIER,
            max_turns=LOCATION_MAX_TURNS,
            max_tokens=LOCATION_MAX_TOKENS,
            provider=LOCATION_PROVIDER,
        )

    # ── handle - the orchestrator-facing entry point ─────────────────────────

    async def handle(self, user_message: str, context: dict) -> ToolResult:
        """Run one targeting request through the agent's own loop.

        The prompt carries profile + current list + the verbatim request; the
        model picks ONE tool. The tool wrapper already rejected empty messages.
        """
        error = validate_run_context(context)
        if error is not None:
            return error

        parent_ctx = context["session_context"]
        product = parent_ctx.setdefault("product_data", {})
        spec = parent_ctx.setdefault("campaign_spec", {})
        place = product.setdefault("place", {})

        stream = context.get("event_stream")
        tool_use_id = context.get("tool_use_id", "")
        auth = context.get("auth")

        # Geocodes on cache miss; stamps coords + country_code onto place.
        location_name = resolve_location_name(product, spec)
        coords = await resolve_coordinates(location_name, place)
        await resolve_country_geo_constant(
            place, (coords or {}).get("country") or "",
            context.get("client_code", ""), build_ds_headers(context),
        )

        sub_session = await build_sub_session(
            parent_ctx, auth, chat_session_id=context.get("session_id", ""),
        )
        country_code = place.get("country_code") or "IN"

        # Launcher owns both AgentCard ends: agent_started here, finished below.
        if stream is not None:
            await pre_emit_agent_started(
                stream, agent_id="location_agent", label="Location Agent",
                parent_tool_use_id=tool_use_id, context=parent_ctx,
            )
        wrapped = LocationPassthroughEventStream(stream) if stream else AgentEventStream()

        status = "success"
        run_started = time.monotonic()
        try:
            await self.run(
                user_message=build_run_prompt(
                    product, location_name or "", country_code, user_message.strip(),
                ),
                session=sub_session,
                event_stream=wrapped,
            )
        except Exception as e:
            status = "failed"
            logger.exception("LocationAgent run failed: %s", e)
        finally:
            if stream is not None:
                try:
                    await stream.emit_agent_finished(
                        "location_agent", status=status,
                        duration_ms=int((time.monotonic() - run_started) * 1000),
                    )
                except Exception:
                    pass  # suppress: AgentCard close is cosmetic; never mask the run's real result

        return build_run_result(sub_session, product, spec)

    # ── framework hook (called by BaseAgent, not by name) ─────────────────

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        """Expose the (sub-)session state to the location tools."""
        ctx = super().build_tool_context(session)
        ctx["session_context"] = session.context
        ctx["_session"] = session
        if session.auth:
            ctx["auth"] = session.auth
        return ctx


@functools.cache
def get_location_agent() -> LocationAgent:
    """Shared LocationAgent singleton."""
    agent = LocationAgent()
    logger.info("LocationAgent created with %d tools", len(LOCATION_AGENT_TOOLS))
    return agent
