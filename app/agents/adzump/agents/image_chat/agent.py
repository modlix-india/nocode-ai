from __future__ import annotations

import logging
import re

from app.core.agent import BaseAgent
from app.core.session import BaseSession
from app.core.streaming import (
    AgentEventStream,
    current_agent_id,
    pre_emit_agent_started,
)
from app.core.tools.base import ToolResult
from app.agents.adzump.agents.image_chat.context import build_image_context
from app.agents.adzump.agents.image_chat.models import ImageChatSession

logger = logging.getLogger(__name__)

MAX_TURNS = 1
MAX_TOKENS = 16384


class ImageAgent(BaseAgent):
    """Pure image generation agent backed by Gemini Imagen.

    Operates with **no tools** — it is a straight-through single-turn
    conversational image generator. Multi-turn conversation is achieved
    by the caller (CreativeAgent) calling ``handle()`` repeatedly with
    the same ``ImageChatSession``.
    """

    display_name = "Image Designer"

    def __init__(self) -> None:
        super().__init__(
            name="image_agent",
            tools=[],
            context_builder=build_image_context(),
            model_tier="balanced",
            max_turns=MAX_TURNS,
            max_tokens=MAX_TOKENS,
            provider="gemini_imagen",
        )

    async def handle(
        self,
        user_message: str,
        image_session: ImageChatSession,
        brand_logo_url: str | None = None,
        aspect_ratio: str | None = None,
        event_stream: AgentEventStream | None = None,
    ) -> ToolResult:
        """Generate or edit an image through a single-turn conversation.

        Args:
            user_message: The user's text prompt for the image.
            image_session: The per-image conversational session.
            brand_logo_url: Optional brand logo to include as reference.
            aspect_ratio: Override aspect ratio for this session.
            event_stream: Optional SSE stream for preview events.

        Returns:
            ToolResult with the generated image URL.
        """
        if aspect_ratio:
            image_session.aspect_ratio = aspect_ratio

        # Store aspect ratio in session context so the Gemini provider
        # can read it — _resolve_aspect_from_messages needs this to
        # send the correct aspectRatio to the Imagen API.
        image_session.base_session.context["_aspect_ratio"] = image_session.aspect_ratio
        # Also make it available via context_management which is passed
        # to the provider in stream_completion_with_tools.
        self.context_management = {"aspect_ratio": image_session.aspect_ratio}

        if event_stream is not None:
            await pre_emit_agent_started(
                event_stream,
                agent_id="image_agent",
                label="Image Designer",
            )

        try:
            image_blocks = []

            # If we already generated an image in this session, this is an edit.
            # We MUST attach the previous image to the current user prompt as a reference
            # image, because Gemini Image-to-Image requires the base image in the current turn.
            prev_image_url = image_session.base_session.context.get(
                "_current_image_url"
            )
            if prev_image_url:
                image_blocks.append({"type": "image_source", "url": prev_image_url})

            if brand_logo_url:
                image_blocks.append({"type": "image_source", "url": brand_logo_url})

            if not image_blocks:
                image_blocks = None

            await self.run(
                user_message=user_message,
                session=image_session.base_session,
                event_stream=event_stream,
                image_blocks=image_blocks,
            )

            image_url = image_session.base_session.context.get("_current_image_url")
            if image_url:
                image_session.image_count += 1
                return ToolResult(
                    success=True,
                    summary=f"Generated: {image_url}",
                )
            return ToolResult(
                success=True,
                summary="Image conversation completed but no image was generated.",
            )
        except Exception as e:
            logger.exception("ImageAgent handle failed: %s", e)
            return ToolResult(success=False, error=str(e))
        finally:
            if event_stream is not None:
                try:
                    await event_stream.emit_agent_finished(
                        "image_agent",
                        status="success",
                    )
                except Exception:
                    pass

    async def _on_image_generated(
        self,
        image_data: bytes,
        image_mime: str,
        session: BaseSession,
        event_stream: AgentEventStream,
    ) -> str | None:
        """Upload generated image to CDN and emit a preview event.

        Returns the public CDN URL or ``None`` on failure.
        """
        image_url = await self._upload_image(image_data, image_mime, session)
        if image_url:
            await self._emit_image_preview(event_stream, image_url)
        return image_url

    async def _upload_image(
        self,
        image_data: bytes,
        image_mime: str,
        session: BaseSession,
    ) -> str | None:
        """Upload image bytes to CDN via Gateway files API (multipart form)."""
        auth = session.auth
        if not auth:
            logger.warning("ImageAgent upload: no auth context — skipping upload")
            return None

        import httpx
        from app.config import settings

        ext = _mime_to_ext(image_mime)
        prompt_slug = _prompt_slug_from_session(session)
        image_count = session.context.get("_image_count", 0) + 1
        session.context["_image_count"] = image_count
        filename = f"{prompt_slug}_{image_count}{ext}"
        kind = "creatives"

        gateway = settings.GATEWAY_URL.rstrip("/")
        headers = auth.to_headers()
        headers["accept"] = "application/json"
        client_code = auth.client_code

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                await client.post(
                    f"{gateway}/api/files/static/directory/{kind}",
                    headers=headers,
                )
                response = await client.post(
                    f"{gateway}/api/files/static/{kind}?clientCode={client_code}",
                    headers=headers,
                    files={"file": (filename, image_data, image_mime)},
                )
                if response.status_code == 200:
                    data = response.json()
                    upload_url = data.get("url", "")
                    if upload_url:
                        if upload_url.startswith("/"):
                            upload_url = f"{gateway}{upload_url}"
                        elif not upload_url.startswith("http"):
                            upload_url = f"{gateway}/{upload_url}"
                        logger.info("ImageAgent: uploaded to %s", upload_url)
                        session.context["_current_image_url"] = upload_url
                        return upload_url
                    logger.warning("ImageAgent: upload response had no url: %s", data)
                    return None
                else:
                    logger.warning(
                        "ImageAgent: upload failed status=%s body=%s",
                        response.status_code,
                        response.text[:300],
                    )
                    return None
        except Exception as e:
            logger.exception("ImageAgent: upload failed: %s", e)
            return None

    async def _emit_image_preview(
        self, event_stream: AgentEventStream, image_url: str
    ) -> None:
        """Emit an ``image_generated`` SSE data event for the UI."""
        token = current_agent_id.set(self.name)
        try:
            await event_stream.emit_data("image_generated", {"url": image_url})
        except Exception as e:
            logger.warning("ImageAgent: failed to emit preview: %s", e)
        finally:
            current_agent_id.reset(token)


def _mime_to_ext(mime: str) -> str:
    return {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(
        mime, ".png"
    )


def _prompt_slug_from_session(session: BaseSession) -> str:
    """Derive a short slug from the last user message for filename."""
    messages = session.get_messages()
    for msg in reversed(messages):
        content = msg.get("content", "")
        if isinstance(content, str) and content.strip():
            slug = re.sub(r"[^a-zA-Z0-9]+", "_", content.strip())[:30].strip("_")
            return slug.lower() or "image"
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "").strip()
                    if text:
                        slug = re.sub(r"[^a-zA-Z0-9]+", "_", text)[:30].strip("_")
                        return slug.lower() or "image"
    return "image"
