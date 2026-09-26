"""Meta Detailed Targeting Agent

Runs the LLM-based pipeline to discover, enrich, and validate Meta audience
targeting segments for a given ad account.

Workflow (driven by the LLM via 6 tools):
  1. fetch_interests                 - discover interest targeting candidates
  2. fetch_behaviors                 - discover behavior targeting candidates
  3. fetch_demographics              - discover demographic targeting candidates
  4. search_professional_demographics - search job titles, employers, majors
  5. validate_targeting              - validate selected candidates, stash final result
  6. delete_targeting_segment        - remove segments by name, ID, or category
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.core.agent import BaseAgent
from app.core.session import BaseSession, AuthContext
from app.core.streaming import AgentEventStream, current_agent_id
from app.agents.adzump.agents.meta_detailed_targeting.subagent_event_stream import (
    MetaPassthroughEventStream,
)
from app.agents.adzump.agents.meta_detailed_targeting.models import (
    MetaTargetingSuggestionResult,
    TargetingEntity,
)
from app.agents.adzump.agents.meta_detailed_targeting.context import (
    build_detailed_targeting_context,
)
from app.agents.adzump.agents.meta_detailed_targeting.tools import INNER_TARGETING_TOOLS
from app.config import settings

logger = logging.getLogger(__name__)

# Turn budget for the LLM loop.
_MAX_TURNS = 20


# Sub-session builder
async def build_sub_session(
    parent_ctx: dict[str, Any],
    auth: AuthContext,
    ad_account_id: str,
    parent_session_id: str,
) -> BaseSession:
    """Create a sub-session with shared context refs but isolated message history.

    Shared dict refs let the sub-agent's tools write through to the parent
    (same objects in memory). The sub-agent's message history stays isolated.
    """
    sub_session = BaseSession(agent_name="detailed_targeting")
    await sub_session.get_or_create(None, auth)

    # Share parent context directly in RAM
    sub_session.context = parent_ctx
    sub_session.context["ad_account_id"] = ad_account_id
    sub_session.context["_parent_session_id"] = parent_session_id

    # Pre-populate candidate pool with existing targeting entities
    parent_targeting = parent_ctx.get("detailed_targeting") or {}
    existing_entities = parent_targeting.get("entities") or []
    pool: dict[str, TargetingEntity] = parent_ctx.setdefault("_candidate_pool", {})
    for raw_ent in existing_entities:
        parsed_ent = TargetingEntity.from_meta(raw_ent)
        if parsed_ent and parsed_ent.id:
            pool[str(parsed_ent.id)] = parsed_ent.model_dump()

    return sub_session


class DetailedTargetingAgent(BaseAgent):
    """Agent that discovers, curates, and validates Meta targeting segments.

    The agent provides a system prompt and 5 tools. The LLM orchestrates the
    full pipeline; Python only handles session setup, context injection, result
    assembly, and UI emission.
    """

    display_name = "Targeting Analyst"
    _instance: DetailedTargetingAgent | None = None

    def __init__(self) -> None:
        from app.agents.adzump.agents.meta_detailed_targeting.tools.detailed_targeting_tool import (
            delete_targeting_segment,
        )
        combined_tools = list(INNER_TARGETING_TOOLS) + [delete_targeting_segment]

        context = build_detailed_targeting_context()
        super().__init__(
            name="detailed_targeting",
            tools=combined_tools,
            context_builder=context,
            model_tier=settings.AGENT_MODEL_TIER,
            max_turns=_MAX_TURNS,
            max_tokens=settings.AGENT_MAX_TOKENS,
            provider=settings.ADZUMP_PROVIDER,
        )

    @classmethod
    def get_instance(cls) -> DetailedTargetingAgent:
        if cls._instance is None:
            cls._instance = cls()
            logger.info("DetailedTargetingAgent instance initialised")
        return cls._instance

    # Override: forward session.context into tool execution context dict
    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        """Extend the base tool context with auth and ad_account_id.

        BaseAgent.build_tool_context only injects session_id, headers,
        client_code, and app_code. The DetailedTargetingAgent tools also
        need `auth` (the full AuthContext) and `ad_account_id`.
        We resolve auth directly from session.auth to prevent persisting bearer tokens.
        """
        ctx = super().build_tool_context(session)
        ctx["session_context"] = session.context
        # Expose the live session object so the sub-agent tools can resolve
        # the parent session-level data when called from this sub-agent loop.
        ctx["_session"] = session
        # Read auth from session.auth directly (never from session.context) to prevent token leaks
        if getattr(session, "auth", None):
            ctx["auth"] = session.auth
        elif "auth" in session.context:
            ctx["auth"] = session.context["auth"]
        if "ad_account_id" in session.context:
            ctx["ad_account_id"] = session.context["ad_account_id"]
        return ctx

    # Override: Inject dynamic business context (summary, spec) into LLM system prompt
    async def build_dynamic_context(self, session: BaseSession) -> str:
        """Serialize and inject business context (summary, spec, current targeting) into dynamic prompt context."""
        ctx = await super().build_dynamic_context(session)
        parent_ctx = session.context.get("parent_session_context") or session.context

        summary = (
            parent_ctx.get("product_data", {}).get("summary")
            or parent_ctx.get("product_profile", {}).get("summary")
            or ""
        )
        spec = parent_ctx.get("campaign_spec") or {}

        context_str = f"BUSINESS DESCRIPTION:\n{summary}\n\n"
        context_str += f"CAMPAIGN OBJECTIVE: {spec.get('objective', 'N/A')}\n"

        # Inject current targeting state if present
        targeting = parent_ctx.get("detailed_targeting") or {}
        entities = targeting.get("entities") or []
        if entities:
            context_str += "\nCURRENT TARGETING:\n"
            for entity in entities:
                ent_id = entity.get("id")
                ent_name = entity.get("name")
                ent_type = entity.get("type")
                context_str += f"- ID: {ent_id} | Name: {ent_name} | Type: {ent_type}\n"

        return f"{ctx}\n{context_str}".strip()

    # Public entry point
    async def recommend(
        self,
        session_id: str,
        ad_account_id: str,
        parent_event_stream: AgentEventStream,
        auth: AuthContext,
        parent_session_context: dict[str, Any],
        parent_tool_use_id: str = "",
        user_query: str = "",
    ) -> tuple[MetaTargetingSuggestionResult, str]:

        run_start = time.monotonic()

        # 1. Create throwaway sub-session with shared context
        sub_session = await build_sub_session(
            parent_ctx=parent_session_context,
            auth=auth,
            ad_account_id=ad_account_id,
            parent_session_id=session_id,
        )

        ctx_token = current_agent_id.set("detailed_targeting")
        wrapped_stream = MetaPassthroughEventStream(
            parent_event_stream, parent_tool_use_id
        )

        try:
            logger.info(
                "[DetailedTargeting] Starting LLM loop — ad_account_id=%s",
                ad_account_id,
            )

            # Ensure the context builder is loaded
            await self.context_builder.load()

            parent_session_context.pop("_validated_targeting", None)

            # 2. Run the LLM loop
            await self.run(
                user_query,
                session=sub_session,
                event_stream=wrapped_stream,
            )

            logger.info(
                "[DetailedTargeting] LLM loop finished — ad_account_id=%s",
                ad_account_id,
            )

            # Extract the LLM's explanation from its last assistant message
            explanation = ""
            for msg in reversed(getattr(sub_session, "messages", [])):
                if msg.get("role") == "assistant":
                    content = msg.get("content")
                    if isinstance(content, str):
                        explanation = content
                        break
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                explanation += block.get("text", "") + "\n"
                        if explanation.strip():
                            break

            # 3. Read the stashed result
            # validate_targeting writes to _validated_targeting.
            # delete_targeting_segment writes directly to detailed_targeting.
            # We check both: _validated_targeting first (recommend run), then
            # detailed_targeting (delete run).
            raw = sub_session.context.get("_validated_targeting")
            if raw is None:
                # Delete run: the LLM called delete_targeting_segment which
                # updated detailed_targeting directly. Read the post-delete state.
                dt = parent_session_context.get("detailed_targeting")
                if dt and isinstance(dt, dict) and dt.get("entities") is not None:
                    raw = dt
                    logger.info("[DetailedTargeting] Delete run detected — using post-delete state.")
                else:
                    logger.warning("[DetailedTargeting] Neither validate_targeting nor delete ran.")
                    raise RuntimeError("Targeting validation did not complete successfully.")

            # 4. Assemble MetaTargetingSuggestionResult
            final_result = MetaTargetingSuggestionResult.from_dict(raw)

            # Stash for parent session visibility
            parent_session_context["detailed_targeting"] = final_result.model_dump()

            # 5. Emit the targeting craft to the UI
            if explanation.strip() and parent_event_stream:
                await parent_event_stream.emit_text(explanation.strip())

            craft_id = f"detailed_targeting_{session_id}"
            search_results = parent_session_context.get("detailed_targeting_search_results") or []
            await self._emit_targeting_craft(
                stream=wrapped_stream,
                craft_id=craft_id,
                title="Meta Targeting Suggestions",
                result=final_result,
                search_results=search_results,
                message_id=parent_tool_use_id,
            )

            summary = f"entities={len(final_result.entities)}"
            logger.info("[DetailedTargeting] Done — %s", summary)
            await self._emit_finished(
                parent_event_stream, run_start, sub_session, "success", summary
            )

            return final_result, explanation.strip()

        except Exception as e:
            logger.exception("[DetailedTargeting] Pipeline failed")
            await self._emit_finished(
                parent_event_stream, run_start, sub_session, "error", str(e)
            )
            raise
        finally:
            parent_session_context.pop("_candidate_pool", None)
            parent_session_context.pop("_parent_session_id", None)
            parent_session_context.pop("_validated_targeting", None)
            current_agent_id.reset(ctx_token)

    # UI helpers
    @staticmethod
    async def _emit_finished(
        parent_event_stream: AgentEventStream | None,
        run_start: float,
        sub_session: BaseSession,
        status: str,
        summary: str = "",
    ) -> None:
        """Emit agent_finished event for the sub-agent card."""
        if parent_event_stream is None:
            return
        try:
            duration_ms = int((time.monotonic() - run_start) * 1000)
            usage = sub_session.total_usage or {}
            await parent_event_stream.emit_agent_finished(
                agent_id="detailed_targeting",
                status=status,
                duration_ms=duration_ms,
                tokens_in=int(usage.get("input_tokens", 0)),
                tokens_out=int(usage.get("output_tokens", 0)),
                step_count=1,
                summary=summary,
            )
        except Exception:
            logger.exception("Failed to emit finished event for detailed targeting")

    async def _emit_targeting_craft(
        self,
        stream: AgentEventStream,
        craft_id: str,
        title: str,
        result: MetaTargetingSuggestionResult,
        search_results: list[dict] | None = None,
        message_id: str = "",
    ) -> None:
        """Emit a targeting_manager craft block to the UI."""

        def _format_size(item: TargetingEntity) -> str:
            if item.audience_size_lower_bound and item.audience_size_upper_bound:
                return f"{item.audience_size_lower_bound:,} – {item.audience_size_upper_bound:,}"
            if item.audience_size_lower_bound:
                return f"Over {item.audience_size_lower_bound:,}"
            return ""

        def _serialize(item: TargetingEntity) -> dict:
            return {
                "id": item.id,
                "name": item.name,
                "size": _format_size(item),
                "path": item.path or [],
                "description": item.description or "",
                "type": item.type,
                "audience_size_lower_bound": item.audience_size_lower_bound,
                "audience_size_upper_bound": item.audience_size_upper_bound,
            }

        blocks = [
            {
                "type": "targeting_manager",
                "entities": [_serialize(e) for e in result.entities],
                "searchResults": search_results or [],
            }
        ]
        await stream.emit_craft(
            craft_id=craft_id, title=title, blocks=blocks, message_id=message_id, append=False
        )


def get_detailed_targeting_agent() -> DetailedTargetingAgent:
    """Convenient accessor used by tool definitions in detailed_targeting_tool.py."""
    return DetailedTargetingAgent.get_instance()
