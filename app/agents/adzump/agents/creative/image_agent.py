from __future__ import annotations

import logging

from app.config import settings
from app.core.agent import BaseAgent
from app.core.context import BaseContext
from app.core.session import AuthContext
from app.core.streaming import AgentEventStream
from app.services.creative_providers import get_creative_provider
from app.agents.adzump.agents.creative.models import ImageBrief, ImageResult

logger = logging.getLogger(__name__)


class ImageAgent(BaseAgent):
    """Single-shot image generation without vision critique/repair loop.

    Called internally by CreativeAgent.
    """

    display_name = "Image Generator"

    _instance: ImageAgent | None = None

    def __init__(self) -> None:
        context = BaseContext(
            doc_paths=[], static_prefix="You are an image generation agent."
        )
        context._cached_static_text = context._static_prefix
        super().__init__(
            name="image_agent",
            tools=[],
            context_builder=context,
            model_tier="balanced",
            max_turns=1,
            max_tokens=2048,
            provider=getattr(settings, "ADZUMP_PROVIDER", settings.LLM_PROVIDER),
            context_management=None,
        )

    @classmethod
    def get_instance(cls) -> ImageAgent:
        if cls._instance is None:
            cls._instance = cls()
            logger.info("ImageAgent created (single-shot, no tools, critique disabled)")
        return cls._instance

    async def generate(
        self,
        brief: ImageBrief,
        auth: AuthContext,
        event_stream: AgentEventStream | None = None,
        logo_bytes: bytes | None = None,
        logo_mime: str | None = None,
        base_image_bytes: bytes | None = None,
        base_image_mime: str | None = None,
    ) -> ImageResult:
        """Generate an image using the configured image provider."""
        provider = get_creative_provider()
        gen_result = await provider.generate(
            prompt=brief.prompt,
            width=brief.width,
            height=brief.height,
            aspect_ratio=brief.aspect_ratio,
            logo_bytes=logo_bytes,
            logo_mime=logo_mime,
            base_image_bytes=base_image_bytes,
            base_image_mime=base_image_mime,
        )

        return ImageResult(
            image=gen_result.image,
            mime_type=gen_result.mime_type,
            prompt=brief.prompt,
        )

    async def edit(
        self,
        brief: ImageBrief,
        messages: list[dict],
        auth: AuthContext,
        event_stream: AgentEventStream | None = None,
    ) -> ImageResult:
        """Edit an existing image using the configured image provider."""
        provider = get_creative_provider()
        gen_result = await provider.edit(
            messages=messages,
            width=brief.width,
            height=brief.height,
            aspect_ratio=brief.aspect_ratio,
        )

        return ImageResult(
            image=gen_result.image,
            mime_type=gen_result.mime_type,
            prompt=brief.prompt,
        )


def get_image_agent() -> ImageAgent:
    return ImageAgent.get_instance()
