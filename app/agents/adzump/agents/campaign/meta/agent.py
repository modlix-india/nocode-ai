"""Meta Detailed Targeting Agent

Runs the LLM-based pipeline to discover, enrich, and validate Meta audience
targeting segments for a given ad account.

Workflow (driven by the LLM via the 4 tools in tools/targeting_tools.py):
  1. search_targeting  – find seed segment IDs from product brief keywords
  2. browse_targeting  – fetch full segment catalog for each category (×3)
  3. suggest_related   – enrich interest pool with related segments
  4. validate_targeting – validate candidates, apply limits, stash final result
"""

from __future__ import annotations

import logging
import time
from typing import Any, List

from app.core.agent import BaseAgent
from app.core.session import BaseSession, AuthContext
from app.core.streaming import AgentEventStream, current_agent_id
from app.agents.adzump.agents.campaign.meta.subagent_event_stream import (
    MetaPassthroughEventStream,
)
from app.agents.adzump.agents.campaign.meta.context import (
    build_detailed_targeting_context,
)
from app.agents.adzump.agents.campaign.meta.models import (
    MetaTargetingSuggestionResult,
    TargetingEntity,
)
from app.agents.adzump.agents.campaign.meta.tools import TARGETING_TOOLS
from app.config import settings

logger = logging.getLogger(__name__)

# Turn budget for the LLM loop.
# Pipeline needs at minimum: search(1) + browse×3(3) + suggest(1) + validate(1) = 6 calls.
# Add reasoning turns, retries, and curate step — 25 gives comfortable headroom.
_MAX_TURNS = 12


class DetailedTargetingAgent(BaseAgent):
    """Agent that discovers, curates, and validates Meta targeting segments.

    The agent provides a system prompt and 4 tools. The LLM orchestrates the
    full pipeline; Python only handles session setup, context injection, result
    assembly, and UI emission.
    """

    display_name = "Targeting Analyst"
    _instance: DetailedTargetingAgent | None = None

    def __init__(self) -> None:
        context = build_detailed_targeting_context()
        # Pre-warm the static context to avoid an extra load() call at runtime
        context._cached_static_text = context._static_prefix
        super().__init__(
            name="detailed_targeting",
            tools=TARGETING_TOOLS,
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

    # ------------------------------------------------------------------
    # Override: forward session.context into tool execution context dict
    # ------------------------------------------------------------------
    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        """Extend the base tool context with auth and ad_account_id.

        BaseAgent.build_tool_context only injects session_id, headers,
        client_code, and app_code. The DetailedTargetingAgent tools also
        need `auth` (the full AuthContext) and `ad_account_id`.
        We inject both from session.context where recommend() stashed them.
        """
        ctx = super().build_tool_context(session)
        ctx["session_context"] = session.context
        # Forward auth and ad_account_id stored by recommend() so every tool
        # can reach the Meta API without needing them as LLM-supplied params.
        if "auth" in session.context:
            ctx["auth"] = session.context["auth"]
        if "ad_account_id" in session.context:
            ctx["ad_account_id"] = session.context["ad_account_id"]
        if "parent_session_context" in session.context:
            ctx["parent_session_context"] = session.context["parent_session_context"]
        return ctx

    # ------------------------------------------------------------------
    # Override: Inject dynamic business context (summary, spec) into LLM system prompt
    # ------------------------------------------------------------------
    async def build_dynamic_context(self, session: BaseSession) -> str:
        """Serialize and inject the stashed parent session context (business summary
        and campaign objective/location) directly into the dynamic prompt context.
        """
        ctx = await super().build_dynamic_context(session)
        parent_ctx = session.context.get("parent_session_context") or {}

        summary = (
            parent_ctx.get("product_data", {}).get("summary")
            or parent_ctx.get("product_profile", {}).get("summary")
            or ""
        )
        spec = parent_ctx.get("campaign_spec") or {}

        context_str = f"BUSINESS DESCRIPTION:\n{summary}\n\n"
        context_str += f"CAMPAIGN OBJECTIVE: {spec.get('objective', 'N/A')}\n"
        return f"{ctx}\n{context_str}".strip()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
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

        # ------------------------------------------------------------------
        # 1. Create a sub-session and inject the required context
        # ------------------------------------------------------------------
        sub_session = BaseSession(agent_name="detailed_targeting")
        await sub_session.get_or_create(None, auth)

        # CRITICAL: inject auth and ad_account_id so every tool can resolve them
        sub_session.context["auth"] = auth
        sub_session.context["ad_account_id"] = ad_account_id
        sub_session.context["parent_session_context"] = parent_session_context

        ctx_token = current_agent_id.set("detailed_targeting")
        wrapped_stream = MetaPassthroughEventStream(
            parent_event_stream, parent_tool_use_id
        )

        try:
            logger.info(
                "[DetailedTargeting] Starting LLM loop — ad_account_id=%s",
                ad_account_id,
            )

            # ------------------------------------------------------------------
            # 2. Run the LLM loop
            #    The LLM will call the 4 tools in order; validate_targeting (the
            #    last tool) stashes the final dict into sub_session.context.
            # ------------------------------------------------------------------
            await self.run(
                user_query,  # user message driven by query
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

            # ------------------------------------------------------------------
            # 3. Read the stashed result
            #    validate_targeting writes {"interests": [...], "demographics": [...],
            #    "behaviors": [...]} into sub_session.context["detailed_targeting"].
            # ------------------------------------------------------------------
            raw: dict[str, Any] = sub_session.context.get("detailed_targeting") or {}

            if not raw or not any(raw.values()):
                logger.warning(
                    "[DetailedTargeting] validate_targeting result not found in context; "
                    "falling back to scanning session messages"
                )
                from app.agents.adzump._shared import extract_json

                for msg in reversed(getattr(sub_session, "messages", [])):
                    if msg.get("role") == "assistant":
                        content = msg.get("content")
                        if isinstance(content, str):
                            raw = extract_json(content) or {}
                        elif isinstance(content, list):
                            for block in content:
                                if (
                                    isinstance(block, dict)
                                    and block.get("type") == "text"
                                ):
                                    raw = extract_json(block.get("text", "")) or {}
                                    if raw:
                                        break
                        if raw and any(raw.values()):
                            break

            if not raw or not any(raw.values()):
                logger.warning(
                    "[DetailedTargeting] No targeting results found after fallback scan"
                )

            # ------------------------------------------------------------------
            # 4. Assemble MetaTargetingSuggestionResult
            # ------------------------------------------------------------------
            def _to_entities(items: list[dict]) -> list[TargetingEntity]:
                out: list[TargetingEntity] = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    try:
                        out.append(TargetingEntity(**item))
                    except Exception:
                        logger.debug("Skipping malformed entity: %r", item)
                return out

            final_result = MetaTargetingSuggestionResult(
                interests=_to_entities(raw.get("interests", [])),
                demographics=_to_entities(raw.get("demographics", [])),
                behaviors=_to_entities(raw.get("behaviors", [])),
            )

            # Stash for parent session visibility
            parent_session_context["detailed_targeting"] = final_result.model_dump()
            sub_session.context["detailed_targeting"] = final_result.model_dump()

            # ------------------------------------------------------------------
            # 5. Emit the targeting craft to the UI
            # ------------------------------------------------------------------
            if explanation.strip() and parent_event_stream:
                await parent_event_stream.emit_text(explanation.strip())

            craft_id = f"detailed_targeting_{session_id}"
            await self._emit_targeting_craft(
                stream=wrapped_stream,
                craft_id=craft_id,
                title="Meta Targeting Suggestions",
                result=final_result,
            )

            summary = (
                f"interests={len(final_result.interests)} "
                f"demographics={len(final_result.demographics)} "
                f"behaviors={len(final_result.behaviors)}"
            )
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
            current_agent_id.reset(ctx_token)

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------
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
        search_results: List[dict] | None = None,
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
                "category": item.category,
                "audience_size_lower_bound": item.audience_size_lower_bound,
                "audience_size_upper_bound": item.audience_size_upper_bound,
            }

        serialized_search = []
        for r in search_results or []:
            try:
                entity = TargetingEntity(**r) if isinstance(r, dict) else r
                serialized_search.append(_serialize(entity))
            except Exception:
                logger.warning("Failed to serialize search result item: %r", r)

        blocks = [
            {
                "type": "targeting_manager",
                "interests": [_serialize(i) for i in result.interests],
                "demographics": [_serialize(d) for d in result.demographics],
                "behaviors": [_serialize(b) for b in result.behaviors],
                "search_results": serialized_search,
                "searchResults": serialized_search,
            }
        ]
        await stream.emit_craft(
            craft_id=craft_id, title=title, blocks=blocks, append=False
        )


def get_detailed_targeting_agent() -> DetailedTargetingAgent:
    """Convenient accessor used by tool definitions in detailed_targeting_tool.py."""
    return DetailedTargetingAgent.get_instance()
