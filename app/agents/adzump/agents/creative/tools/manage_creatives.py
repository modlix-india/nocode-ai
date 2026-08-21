from __future__ import annotations

import logging

from app.core.session import BaseSession
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.agents.image_chat.agent import ImageAgent
from app.agents.adzump.agents.image_chat.models import ImageChatSession

logger = logging.getLogger(__name__)

IMAGE_SESSIONS_KEY = "_image_sessions"


async def _manage_creatives(params: dict, context: dict) -> ToolResult:
    """Orchestrate ImageAgent session lifecycle.

    Called by the CreativeAgent LLM when image generation or editing
    is needed.  Manages per-image BaseSession isolation and delegates
    to ImageAgent for the actual Gemini call.
    """
    user_message = (params.get("user_message") or "").strip()
    image_id: str | None = params.get("image_id")
    aspect_ratio: str = params.get("aspect_ratio", "1:1")
    brand_logo_url: str | None = params.get("brand_logo_url")
    base_image_url: str | None = params.get("base_image_url")

    if not user_message:
        return ToolResult(
            success=False,
            error="manage_creatives requires a `user_message` — describe the image to create or edit.",
        )

    parent_ctx = context.get("session_context") or {}

    # If the orchestrator LLM already resolved the image_id (via the outer
    # manage_creatives tool), use it as a fallback when the CreativeAgent
    # LLM didn't explicitly pass one in its inner tool call.
    if not image_id:
        image_id = parent_ctx.get("_requested_image_id")
        parent_ctx.pop("_requested_image_id", None)
    auth = context.get("auth")
    stream = context.get("event_stream")

    image_sessions: dict = parent_ctx.setdefault(IMAGE_SESSIONS_KEY, {})
    agent = ImageAgent()

    if image_id and image_id in image_sessions:
        info = image_sessions[image_id]
        # Restore the prior image session so Gemini sees the full conversation
        # history (previous image + all edits). The session messages are loaded
        # via _restore_conversation_history inside get_or_create.
        base_session = BaseSession(agent_name="image_agent")
        await base_session.get_or_create(info["session_id"], auth)
        base_session.context = parent_ctx
        # Preserve the original aspect ratio unless the caller explicitly overrides it.
        effective_ratio = info.get("aspect_ratio") or aspect_ratio
        image_session = ImageChatSession(
            base_session=base_session,
            aspect_ratio=effective_ratio,
            image_count=info.get("image_count", 0),
        )
        logger.info(
            "manage_creatives: editing image_id=%s session=%s restored_messages=%d",
            image_id,
            info["session_id"],
            len(base_session.messages),
        )
    else:
        base_session = BaseSession(agent_name="image_agent")
        await base_session.get_or_create(None, auth)
        base_session.context = parent_ctx
        image_session = ImageChatSession(
            base_session=base_session,
            aspect_ratio=aspect_ratio,
        )
        # Use len+1 to avoid collisions with concurrent generations; a more
        # robust key would include a timestamp, but len is sufficient here.
        image_id = f"img_{len(image_sessions) + 1}"
        image_sessions[image_id] = {
            "session_id": base_session.session_id,
            "aspect_ratio": aspect_ratio,
            "status": "generating",
            "image_count": 0,
        }
        logger.info(
            "manage_creatives: new image_id=%s session=%s",
            image_id,
            base_session.session_id,
        )

    result = await agent.handle(
        user_message=user_message,
        image_session=image_session,
        brand_logo_url=brand_logo_url,
        base_image_url=base_image_url,
        aspect_ratio=aspect_ratio,
        event_stream=stream,
    )

    if image_id in image_sessions:
        info = image_sessions[image_id]
        info["status"] = "done" if result.success else "failed"
        info["image_count"] = image_session.image_count
        if result.success and result.summary:
            import re as _re

            m = _re.search(r"https?://\S+", result.summary)
            if m:
                info["current_image_url"] = m.group()

    logger.info(
        "manage_creatives: image_id=%s success=%s summary=%r",
        image_id,
        result.success,
        result.summary,
    )
    return result


manage_creatives = ToolDefinition(
    name="manage_creatives",
    description=(
        "Generate or edit an image.  Creates a new image from a text prompt, "
        "or edits an existing image by referring to its ``image_id``. "
        "Pass the user's verbatim description of what the image should look like."
    ),
    display_name="Manage Creatives",
    parameters=[
        ToolParameter(
            name="user_message",
            type="string",
            description="The image prompt or edit instruction — user's verbatim text.",
            required=True,
        ),
        ToolParameter(
            name="image_id",
            type="string",
            description=(
                "Optional.  ID of an existing image to edit (e.g. ``img_1``). "
                "Omit to create a new image."
            ),
            required=False,
        ),
        ToolParameter(
            name="aspect_ratio",
            type="string",
            description="Image aspect ratio (e.g. ``1:1``, ``16:9``, ``4:5``, ``9:16``). Default ``1:1``.",
            required=False,
        ),
        ToolParameter(
            name="brand_logo_url",
            type="string",
            description="Optional URL of the brand logo to include as reference.",
            required=False,
        ),
        ToolParameter(
            name="base_image_url",
            type="string",
            description="Optional URL of a product image to use as the base for the first generation.",
            required=False,
        ),
    ],
    execute=_manage_creatives,
)
