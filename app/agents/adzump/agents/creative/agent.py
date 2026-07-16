from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

from app.core.agent import BaseAgent
from app.core.session import AuthContext, BaseSession
from app.core.streaming import AgentEventStream, pre_emit_agent_started
from app.core.tools.base import ToolResult
from app.agents.adzump.agents.creative.context import build_creative_context
from app.agents.adzump.agents.creative.tools import CREATIVE_TOOLS
from app.config import settings

logger = logging.getLogger(__name__)

CREATIVE_PROVIDER = getattr(settings, "ADZUMP_PROVIDER", settings.LLM_PROVIDER)
CREATIVE_MODEL_TIER = "balanced"
CREATIVE_MAX_TURNS = 15
CREATIVE_MAX_TOKENS = 8192
CREATIVE_SESSION_KEY = "_creative_session_id"


class CreativeAgent(BaseAgent):
    """Requirements-gathering agent that hands off image generation to ImageAgent.

    Its LLM (Anthropic/OpenAI) converses with the user to understand
    image requirements, then calls the ``manage_creatives`` internal tool
    to hand off to ImageAgent (Gemini) for actual image generation.
    """

    display_name = "Creative Designer"

    def __init__(self) -> None:
        super().__init__(
            name="creative_agent",
            tools=CREATIVE_TOOLS,
            context_builder=build_creative_context(),
            model_tier=CREATIVE_MODEL_TIER,
            max_turns=CREATIVE_MAX_TURNS,
            max_tokens=CREATIVE_MAX_TOKENS,
            provider=CREATIVE_PROVIDER,
        )

    async def handle(self, user_message: str, context: dict) -> ToolResult:
        """Handle a creative request through the agent's own LLM loop.

        The CreativeAgent LLM (Anthropic/OpenAI) receives the user's
        request, gathers requirements if needed, and calls the
        ``manage_creatives`` internal tool to hand off to ImageAgent.
        """
        parent_ctx = context.get("session_context") or {}
        auth = context.get("auth")
        stream = context.get("event_stream")
        tool_use_id = context.get("tool_use_id", "")
        chat_session_id = context.get("session_id", "")

        sub_session = await self._get_or_create_sub_session(
            parent_ctx, auth, chat_session_id
        )

        if stream is not None:
            await pre_emit_agent_started(
                stream,
                agent_id="creative_agent",
                label="Creative Designer",
                parent_tool_use_id=tool_use_id,
                context=parent_ctx,
            )

        status = "success"
        logger.info("CreativeAgent.handle starting run. user_message=%r", user_message)
        try:
            await self.run(
                user_message=user_message,
                session=sub_session,
                event_stream=stream,
            )
            logger.info("CreativeAgent.handle run completed successfully.")
        except asyncio.CancelledError:
            status = "cancelled"
            logger.info("CreativeAgent.handle cancelled — building result from context.")
        except Exception as e:
            status = "failed"
            logger.exception("CreativeAgent run failed: %s", e)
        finally:
            if stream is not None:
                try:
                    await stream.emit_agent_finished(
                        "creative_agent",
                        status=status,
                    )
                except Exception:
                    pass

        return self._build_result(sub_session)

    async def _get_or_create_sub_session(
        self,
        parent_ctx: dict,
        auth: AuthContext | None,
        chat_session_id: str,
    ) -> BaseSession:
        sub_session_id = parent_ctx.get(CREATIVE_SESSION_KEY)
        sub_session = BaseSession(agent_name=self.name)
        await sub_session.get_or_create(sub_session_id, auth)
        if not sub_session_id:
            parent_ctx[CREATIVE_SESSION_KEY] = sub_session.session_id
        sub_session.context = parent_ctx
        return sub_session

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        ctx = super().build_tool_context(session)
        ctx["session_context"] = session.context
        ctx["_session"] = session
        if session.auth:
            ctx["auth"] = session.auth
        return ctx

    async def build_dynamic_context(self, session: BaseSession) -> str:
        """Inject product profile + existing image session info for routing."""
        context = session.context or {}
        pdata = context.get("product_data") or {}

        product_name = (
            pdata.get("product_name")
            or context.get("product_name")
            or context.get("brand_name")
            or "the product"
        )
        business_type = (
            pdata.get("business_type")
            or context.get("business_type")
            or "general brand"
        )
        personas = (
            pdata.get("target_personas")
            or context.get("target_personas")
            or "general audience"
        )
        logo_url = _resolve_logo_url(pdata, context)
        location = (
            pdata.get("place", {}).get("address")
            or pdata.get("location")
            or context.get("location")
            or "location not specified"
        )
        pricing = (
            pdata.get("pricing") or context.get("pricing") or "pricing not specified"
        )
        product_summary = (
            pdata.get("summary")
            or context.get("product_summary")
            or context.get("description")
            or "no summary provided"
        )

        lines = [
            "## Product Profile Context",
            f"- Product/Brand Name: {product_name}",
            f"- Business Type: {business_type}",
            f"- Target Personas: {personas}",
            f"- Pricing: {pricing}",
            f"- Location: {location}",
            f"- Logo URL: {logo_url}",
            f"- Product Summary: {product_summary}",
        ]

        # ── Existing image sessions ────────────────────────────────────────────
        # Injected so the LLM can pass the correct image_id when the user asks
        # to edit an existing image instead of generating a brand new one.
        image_sessions: dict = context.get("_image_sessions") or {}
        if image_sessions:
            lines.append("")
            lines.append("## Existing Generated Images")
            lines.append(
                "When the user asks to EDIT an image, pass `image_id=<id>` to "
                "manage_creatives so the existing image is edited, NOT regenerated."
            )
            for img_id, info in image_sessions.items():
                status = info.get("status", "unknown")
                ratio = info.get("aspect_ratio", "?")
                url = info.get("current_image_url") or "(generating...)"
                lines.append(f"- {img_id}: status={status}, ratio={ratio}, url={url}")
        else:
            lines.append("")
            lines.append("## Existing Generated Images")
            lines.append("None yet. Ask the user for the desired format before calling manage_creatives.")

        return "\n".join(lines)


    @staticmethod
    def _build_result(sub_session: BaseSession) -> ToolResult:
        parent_ctx = sub_session.context or {}
        image_sessions: dict = parent_ctx.get("_image_sessions") or {}
        summary_parts: list[str] = []

        if image_sessions:
            summary_parts.append(f"Images generated: {len(image_sessions)}")
            for img_id, info in image_sessions.items():
                url = info.get("current_image_url")
                if url:
                    summary_parts.append(
                        f'![{img_id}]({url})'
                    )
                else:
                    summary_parts.append(
                        f"  {img_id}: {info.get('status', 'unknown')} "
                        f"(ratio={info.get('aspect_ratio', '?')})"
                    )
        else:
            summary_parts.append("Creative session complete.")

        final_text = _extract_last_assistant_text(sub_session) or "Creative session complete."
        return ToolResult(
            success=True,
            summary=f"{final_text}\n\n" + "\n".join(summary_parts),
            audience="both",
        )


def _resolve_logo_url(pdata: dict, context: dict) -> str:
    logos = pdata.get("assets", {}).get("logos") or []
    return (
        (logos[0].get("url") if logos else None)
        or context.get("logo_url")
        or "no logo provided"
    )


def _extract_last_assistant_text(session: BaseSession) -> str | None:
    for m in reversed(session.get_messages()):
        if m.get("role") != "assistant":
            continue
        content = m.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [c.get("text", "") for c in content if c.get("type") == "text"]
            if parts:
                return "\n".join(parts)
    return None


@functools.cache
def get_creative_agent() -> CreativeAgent:
    agent = CreativeAgent()
    logger.info("CreativeAgent created with %d tools", len(CREATIVE_TOOLS))
    return agent
