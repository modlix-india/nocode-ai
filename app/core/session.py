"""Base session for agentic conversations.

Wraps the existing session_manager, context_manager, and token_tracker
to provide a unified interface for the agentic loop.

Conversation messages are kept in-memory in Anthropic format.
The existing DB services handle persistence for analytics/tracking.

Usage:
    session = BaseSession(agent_name="appbuilder")
    await session.get_or_create(session_id, auth)

    history = session.get_messages()
    session.append_user_message("Build a login page")
    session.append_assistant_message(content_blocks, usage)

    await session.persist_turn(user_text, assistant_summary)
    await session.record_token_usage(usage, request_id, model)
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from app.db.models import AiTokenUsageCreate

logger = logging.getLogger(__name__)


@dataclass
class AuthContext:
    """Authentication context passed from the HTTP request."""

    token: str  # Bearer token
    client_code: str
    client_id: int
    user_id: int
    app_code: str

    def to_headers(self) -> dict[str, str]:
        """Build HTTP headers for forwarding to Gateway APIs."""
        return {
            "Authorization": f"Bearer {self.token}" if not self.token.startswith("Bearer") else self.token,
            "clientCode": self.client_code,
            "appCode": self.app_code,
        }


class BaseSession:
    """Wraps session lifecycle and in-memory conversation state.

    Attributes:
        session_id: Unique session identifier.
        agent_name: Name of the agent using this session.
        auth: Authentication context.
        messages: In-memory conversation in Anthropic format.
        total_usage: Accumulated token usage across all turns.
    """

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name
        self.session_id: str = ""
        self.auth: Optional[AuthContext] = None
        self.messages: list[dict[str, Any]] = []
        self.total_usage: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        self._turn_count: int = 0
        self._db_session_created: bool = False

    async def get_or_create(self, session_id: Optional[str], auth: AuthContext) -> str:
        """Initialize the session — reuse existing or create new.

        Args:
            session_id: Existing session ID to resume, or None to create new.
            auth: Authentication context from the HTTP request.

        Returns:
            The session ID (new or existing).
        """
        self.auth = auth

        if session_id:
            self.session_id = session_id
            await self._load_existing_session()
        else:
            await self._create_new_session()

        return self.session_id

    def get_messages(self) -> list[dict[str, Any]]:
        """Return the full conversation history in Anthropic format."""
        return self.messages

    def append_user_message(self, text: str) -> None:
        """Append a user message to the conversation."""
        self.messages.append({
            "role": "user",
            "content": text,
        })

    def append_assistant_message(self, content_blocks: list[dict[str, Any]]) -> None:
        """Append an assistant message (may contain text + tool_use blocks)."""
        self.messages.append({
            "role": "assistant",
            "content": content_blocks,
        })

    def append_tool_results(self, tool_results: list[dict[str, Any]]) -> None:
        """Append tool results as a user message (Anthropic format).

        Args:
            tool_results: List of tool_result content blocks, e.g.:
                [{"type": "tool_result", "tool_use_id": "...", "content": "..."}]
        """
        self.messages.append({
            "role": "user",
            "content": tool_results,
        })

    def accumulate_usage(self, usage: dict[str, Any]) -> None:
        """Add token usage from one LLM call to running totals."""
        for key in self.total_usage:
            self.total_usage[key] += usage.get(key, 0)

    async def persist_turn(self, user_text: str, assistant_summary: str) -> None:
        """Persist a conversation turn to the database for analytics.

        This is a best-effort operation — failures are logged but don't
        stop the agent.
        """
        if not self.auth:
            return

        self._turn_count += 1

        try:
            from app.services.context_manager import get_context_manager
            context_manager = get_context_manager()
            request_id = uuid.uuid4().hex[:8]
            await context_manager.add_turn(
                session_id=self.session_id,
                request_id=request_id,
                turn_number=self._turn_count,
                user_instruction=user_text,
                assistant_summary=assistant_summary,
            )
        except Exception as e:
            logger.warning(f"Failed to persist turn: {e}")

    async def record_token_usage(
        self,
        usage: dict[str, Any],
        request_id: str,
        model: str,
    ) -> None:
        """Record token usage for a single LLM call.

        Best-effort — failures are logged but don't stop the agent.
        """
        if not self.auth:
            return

        try:
            from app.services.token_tracker import get_token_tracker
            from app.config import settings

            tracker = get_token_tracker()
            await tracker.record_usage(AiTokenUsageCreate(
                session_id=self.session_id,
                request_id=request_id,
                client_code=self.auth.client_code,
                client_id=self.auth.client_id,
                user_id=self.auth.user_id,
                agent_type=self.agent_name,
                model=model,
                llm_provider=settings.LLM_PROVIDER,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                latency_ms=usage.get("latency_ms"),
                success=True,
            ))
        except Exception as e:
            logger.warning(f"Failed to record token usage: {e}")

    # ── Internal helpers ────────────────────────────────────────

    async def _create_new_session(self) -> None:
        """Create a new session in the database."""
        try:
            from app.services.session_manager import get_session_manager
            session_manager = get_session_manager()
            session = await session_manager.create_session(
                client_code=self.auth.client_code,
                client_id=self.auth.client_id,
                user_id=self.auth.user_id,
                agent_name=self.agent_name,
                app_code=self.auth.app_code,
            )
            if session:
                self.session_id = session.session_id
                self._db_session_created = True
            else:
                # DB not available — generate ID locally
                self.session_id = f"{self.auth.client_code}_{uuid.uuid4().hex[:8]}"
        except Exception as e:
            logger.warning(f"Failed to create DB session: {e}")
            self.session_id = f"{self.auth.client_code}_{uuid.uuid4().hex[:8]}"

    async def _load_existing_session(self) -> None:
        """Load conversation history from an existing session."""
        try:
            from app.services.session_manager import get_session_manager
            session_manager = get_session_manager()
            session = await session_manager.get_session(self.session_id)
            if session:
                self._turn_count = session.turn_count
                self._db_session_created = True
                # Note: Full Anthropic-format messages are not in the DB.
                # For session resumption, the frontend sends the full history
                # or we rebuild from context_manager summaries.
        except Exception as e:
            logger.warning(f"Failed to load session {self.session_id}: {e}")
