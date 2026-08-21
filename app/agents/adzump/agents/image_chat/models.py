from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.session import BaseSession


@dataclass
class ImageChatSession:
    """Per-image conversational session wrapper.

    Each image conversation gets its own ``BaseSession`` for isolated
    history. The parent CreativeAgent stores these in its session context
    via ``_image_sessions``.
    """

    base_session: BaseSession
    aspect_ratio: str = "1:1"
    image_count: int = 0

    def append_user_blocks(self, blocks: list[dict[str, Any]]) -> None:
        """Append a user message with content blocks (text + image_source)."""
        self.base_session.messages.append({"role": "user", "content": blocks})

    def append_assistant_blocks(self, blocks: list[dict[str, Any]]) -> None:
        """Append an assistant message with content blocks."""
        self.base_session.append_assistant_message(blocks)
