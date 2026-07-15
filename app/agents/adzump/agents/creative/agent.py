from __future__ import annotations

import functools
import logging
import time
from typing import Any

from app.core.agent import BaseAgent
from app.core.session import AuthContext, BaseSession
from app.core.streaming import AgentEventStream, pre_emit_agent_started
from app.core.tools.base import ToolResult
from app.agents.adzump.agents.creative.context import build_creative_context
from app.agents.adzump.agents.creative.models import Creative
from app.agents.adzump.agents.creative.tools import CREATIVE_TOOLS
from app.config import settings

logger = logging.getLogger(__name__)

CREATIVE_PROVIDER = getattr(settings, "ADZUMP_PROVIDER", settings.LLM_PROVIDER)
CREATIVE_MODEL_TIER = "balanced"
CREATIVE_MAX_TURNS = 15
CREATIVE_MAX_TOKENS = 8192
CREATIVE_SESSION_KEY = "_creative_session_id"


def ensure_creatives_hydrated(parent_ctx: dict) -> list[Creative]:
    """Hydrate all serialized dictionaries back to Creative objects in-place."""
    raw_list = parent_ctx.setdefault("_creatives", [])
    for i, item in enumerate(raw_list):
        if not isinstance(item, Creative):
            raw_list[i] = Creative.from_dict(item)
    return raw_list


class CreativeAgent(BaseAgent):
    """Conversational creative generation agent with persistent sub-session."""

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
        """Handle a creative request through the agent's own loop.

        Uses a persistent sub-session (stored in parent context) so the LLM
        remembers the full conversation across multiple handle() calls.
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
        run_started = time.monotonic()
        logger.info("CreativeAgent.handle starting run. user_message=%r", user_message)
        try:
            await self.run(
                user_message=self._build_prompt(parent_ctx, user_message),
                session=sub_session,
                event_stream=stream,
            )
            logger.info("CreativeAgent.handle run completed successfully.")
        except Exception as e:
            status = "failed"
            logger.exception("CreativeAgent run failed: %s", e)
        finally:
            if stream is not None:
                creatives = ensure_creatives_hydrated(parent_ctx)
                preview_markdowns = []
                for c in creatives:
                    if c.status == "done" and c.image_url:
                        logger.info("CreativeAgent found completed creative id=%s label=%s url=%s", c.id, c.format_label, c.image_url)
                        preview_markdowns.append(
                            f'\n![{c.format_label}]({c.image_url}){{style="width: 250px; height: 250px; object-fit: contain; border-radius: 8px; margin: 4px;"}}'
                        )
                if preview_markdowns:
                    logger.info("CreativeAgent emitting preview_markdowns text count=%d", len(preview_markdowns))
                    from app.core.streaming import current_agent_id

                    token = current_agent_id.set(self.name)
                    try:
                        await stream.emit_text("".join(preview_markdowns))
                    except Exception as err:
                        logger.warning(
                            "Failed to emit creative previews to stream: %s", err
                        )
                    finally:
                        current_agent_id.reset(token)
                else:
                    logger.info("CreativeAgent: no completed creatives found to preview.")

                try:
                    await stream.emit_agent_finished(
                        "creative_agent",
                        status=status,
                        duration_ms=int((time.monotonic() - run_started) * 1000),
                    )
                except Exception:
                    pass

        res = self._build_result(sub_session)
        logger.info("CreativeAgent handle returning ToolResult summary_len=%d", len(res.summary or ""))
        return res

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
        """Inject product profile, logo, business type, and target personas into the system context."""
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
        pricing = (
            pdata.get("pricing") or context.get("pricing") or "pricing not specified"
        )

        # Extract location from nested place dict or direct location key
        location = (
            pdata.get("place", {}).get("address")
            or pdata.get("location")
            or context.get("location")
            or "location not specified"
        )

        rera_no = (
            pdata.get("rera_no")
            or context.get("rera_no")
            or "RERA registration not specified"
        )

        # Resolve logo_url from nested assets structure
        logos = pdata.get("assets", {}).get("logos") or []
        logo_url = (
            (logos[0].get("url") if logos else None)
            or context.get("logo_url")
            or "no logo provided"
        )

        personas = (
            pdata.get("target_personas")
            or context.get("target_personas")
            or "general audience"
        )

        product_summary = (
            pdata.get("summary")
            or context.get("product_summary")
            or context.get("description")
            or "no summary provided"
        )

        is_real_estate = any(
            kw in business_type.lower()
            for kw in ("real estate", "property", "apartment", "developer", "builder")
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

        if is_real_estate:
            lines.extend(
                [
                    f"- RERA Registration: {rera_no}",
                    "",
                    "### CRITICAL REAL ESTATE COMPLIANCE RULES:",
                    "Since this is a Real Estate brand, you MUST do the following:",
                    "1. When generating a creative using the `create_creative` tool, you MUST include the exact RERA registration details in the `rera_no` parameter.",
                    "2. You MUST explicitly request in the visual `prompt` that the Project Name, Price, Location, and RERA Registration number (verbatim) are clearly printed and laid out on the final image.",
                    "3. Ensure the 'location' parameter is a concise city/area (e.g. 'Whitefield, Bengaluru').",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "### BRANDING RULES:",
                    "When calling `create_creative`, write the visual `prompt` to describe a clean design layout integrating the logo (Image 1) and background/product scene (Image 2 if provided, or generated from scratch).",
                    "Instruct the model to render the headline and CTA text in clean, professional typography.",
                ]
            )

        return "\n".join(lines)

    async def build_turn_reminder(
        self, session: BaseSession, turn: int = 1
    ) -> str | None:
        """Inject the active image's prompt_history if one is being edited."""
        context = session.context
        active_id = context.get("_active_image_id")
        if not active_id:
            return None
        creatives = ensure_creatives_hydrated(context)
        target = next(
            (c for c in creatives if c.id == active_id),
            None,
        )
        if not target:
            return None
        history = "\n".join(f"{i}. {e}" for i, e in enumerate(target.prompt_history, 1))
        return (
            f"Active image: {target.format_label} ({target.id})\n"
            f"Prompt history:\n{history or '(none)'}\n"
        )

    @staticmethod
    def _build_prompt(parent_ctx: dict, user_message: str) -> str:
        """Build the prompt with current creative context."""
        creatives = ensure_creatives_hydrated(parent_ctx)
        if not creatives:
            return user_message
        summary_lines = ["Current creatives:"]
        for c in creatives:
            status_icon = (
                "✓" if c.status == "done" else "⏳" if c.status == "generating" else "✗"
            )
            summary_lines.append(
                f"  {status_icon} [{c.id}] {c.format_label} — {c.prompt[:80]}..."
            )
        summary_lines.append("")
        summary_lines.append(f"User: {user_message}")
        return "\n".join(summary_lines)

    @staticmethod
    def _build_result(sub_session: BaseSession) -> ToolResult:
        parent_ctx = sub_session.context or {}
        creatives = ensure_creatives_hydrated(parent_ctx)

        # Helper helper to map aspect ratio to labels matching tools.py _format_label
        def map_ar_label(width: int, height: int) -> str:
            ratio = width / height
            if abs(ratio - 1.0) < 0.05:
                return "square"
            if abs(ratio - 4 / 5) < 0.05:
                return "portrait"
            if abs(ratio - 16 / 9) < 0.05:
                return "landscape"
            if abs(ratio - 9 / 16) < 0.05:
                return "story"
            if abs(ratio - 1.91) < 0.05:
                return "social"
            if width == height:
                return "square"
            elif height > width:
                return "portrait"
            else:
                return "landscape"

        # Sync back to campaign_spec["ad_copy"] so the UI and orchestrator can read them
        # Group by (headline, description, cta) key to aggregate different size URLs under creative_urls
        grouped_copies = {}
        for c in creatives:
            if c.status == "done" and c.image_url:
                headline = c.headline or ""
                description = c.description or ""
                cta = c.cta or ""
                key = (headline, description, cta)

                ar_label = map_ar_label(c.width, c.height)
                if key not in grouped_copies:
                    grouped_copies[key] = {
                        "headline": headline,
                        "description": description,
                        "cta": cta,
                        "creative_type": "image",
                        "creative_urls": {},
                    }
                grouped_copies[key]["creative_urls"][ar_label] = c.image_url

        ad_copy_list = list(grouped_copies.values())

        if ad_copy_list:
            campaign_spec = parent_ctx.setdefault("campaign_spec", {})
            campaign_spec["ad_copy"] = ad_copy_list
            parent_ctx.setdefault("product_profile", {})["creative_generated"] = True

        final_text = ""
        for m in reversed(sub_session.get_messages()):
            if m.get("role") != "assistant":
                continue
            content = m.get("content")
            if isinstance(content, str):
                final_text = content
                break
            if isinstance(content, list):
                parts = [c.get("text", "") for c in content if c.get("type") == "text"]
                if parts:
                    final_text = "\n".join(parts)
                    break
        if not final_text:
            final_text = "Creative session complete."

        # Programmatically append image previews so they are guaranteed to render in the UI chat bubble
        preview_markdowns = []
        for c in creatives:
            if c.status == "done" and c.image_url:
                preview_markdowns.append(
                    f'\n![{c.format_label}]({c.image_url}){{style="width: 250px; height: 250px; object-fit: contain; border-radius: 8px; margin: 4px;"}}'
                )
        if preview_markdowns:
            final_text += "\n" + "".join(preview_markdowns)

        return ToolResult(
            success=True,
            data={"creatives": [c.to_dict() for c in creatives]},
            summary=final_text[:8000],
            audience="both",
        )


@functools.cache
def get_creative_agent() -> CreativeAgent:
    agent = CreativeAgent()
    logger.info("CreativeAgent created with %d tools", len(CREATIVE_TOOLS))
    return agent
