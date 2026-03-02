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

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from app.db.models import AiTokenUsageCreate

logger = logging.getLogger(__name__)


@dataclass
class AuthContext:
    """Authentication context passed from the HTTP request.

    Attributes:
        app_code: The target application being built/edited.
        access_app_code: The app used to access the AI (appbuilder/sitezump).
            This goes in the ``appCode`` header for backend auth context,
            while ``app_code`` is sent as a request parameter where needed.
    """

    token: str  # Bearer token
    client_code: str
    client_id: int
    user_id: int
    app_code: str
    access_app_code: str = "appbuilder"
    forwarded_host: str = "localhost"
    forwarded_port: str = "80"

    def to_headers(self) -> dict[str, str]:
        """Build HTTP headers for forwarding to Gateway APIs.

        Returns all 5 headers required by backend services
        (matches Java IFeignSecurityService Feign interface).
        The ``appCode`` header is set to the *access* app (appbuilder/sitezump),
        NOT the target app being built.
        """
        return {
            "Authorization": f"Bearer {self.token}" if not self.token.startswith("Bearer") else self.token,
            "X-Forwarded-Host": self.forwarded_host,
            "X-Forwarded-Port": self.forwarded_port,
            "clientCode": self.client_code,
            "appCode": self.access_app_code,
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
        self.context: dict[str, Any] = {}
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

    def append_user_message(self, text: str, image_blocks: list[dict[str, Any]] | None = None) -> None:
        """Append a user message to the conversation.

        Args:
            text: The text content.
            image_blocks: Optional list of image content blocks (Anthropic format).
                Each block should be {"type": "image", "source": {"type": "base64", ...}}.
        """
        if image_blocks:
            content: list[dict[str, Any]] = [{"type": "text", "text": text}]
            content.extend(image_blocks)
            self.messages.append({"role": "user", "content": content})
        else:
            self.messages.append({"role": "user", "content": text})

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

    async def persist_turn(
        self,
        user_text: str,
        assistant_summary: str,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        """Persist a conversation turn to the database for analytics and training.

        Args:
            user_text: The user's message for this turn.
            assistant_summary: Text summary of the assistant's response.
            tool_calls: List of tool call records for this turn. Each entry:
                {"tool": str, "input": dict, "success": bool, "summary": str}

        This is a best-effort operation — failures are logged but don't
        stop the agent.
        """
        if not self.auth:
            return

        self._turn_count += 1

        tool_calls_json: str | None = None
        if tool_calls:
            tool_calls_json = json.dumps(tool_calls)

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
                tool_calls_json=tool_calls_json,
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

    async def complete(self) -> None:
        """Mark session as COMPLETED in the database.

        Best-effort — failures are logged but don't stop the agent.
        """
        if not self.auth:
            return

        try:
            from app.services.session_manager import get_session_manager
            await get_session_manager().complete_session(
                self.session_id, self.auth.user_id if self.auth else None
            )
        except Exception as e:
            logger.warning(f"Failed to complete session: {e}")

    async def set_processing(self) -> None:
        """Mark session as PROCESSING in the database.

        Called at the start of the agent loop so the UI can detect
        an in-progress request after a page refresh.
        Best-effort — failures are logged but don't stop the agent.
        """
        if not self.auth:
            return

        try:
            from app.services.session_manager import get_session_manager
            await get_session_manager().set_session_processing(
                self.session_id, self.auth.user_id if self.auth else None
            )
        except Exception as e:
            logger.warning(f"Failed to set session processing: {e}")

    async def persist_turn_incremental(
        self,
        user_text: str,
        assistant_summary: str,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        """Upsert the current turn state for incremental saves.

        Called after each LLM + tool cycle so partial progress survives
        disconnects. Uses INSERT ... ON DUPLICATE KEY UPDATE.
        Best-effort — failures are logged but don't stop the agent.
        """
        if not self.auth:
            return

        tool_calls_json: str | None = None
        if tool_calls:
            tool_calls_json = json.dumps(tool_calls)

        try:
            from app.services.context_manager import get_context_manager
            context_manager = get_context_manager()
            request_id = uuid.uuid4().hex[:8]
            await context_manager.upsert_turn(
                session_id=self.session_id,
                request_id=request_id,
                turn_number=self._turn_count + 1,
                user_instruction=user_text,
                assistant_summary=assistant_summary,
                tool_calls_json=tool_calls_json,
            )
        except Exception as e:
            logger.warning(f"Failed to persist incremental turn: {e}")

    async def save_context(self) -> None:
        """Persist the current context dict to the database.

        Best-effort — failures are logged but don't stop the agent.
        """
        if not self.context:
            return

        try:
            from app.services.session_manager import get_session_manager
            context_json = json.dumps(self.context)
            await get_session_manager().update_session_context(
                self.session_id, context_json, self.auth.user_id if self.auth else None
            )
        except Exception as e:
            logger.warning(f"Failed to save session context: {e}")

    # ── Internal helpers ────────────────────────────────────────

    async def _create_new_session(self) -> None:
        """Create a new session in the database."""
        try:
            from app.services.session_manager import get_session_manager
            session_manager = get_session_manager()
            context_json = json.dumps(self.context) if self.context else None
            session = await session_manager.create_session(
                client_code=self.auth.client_code,
                client_id=self.auth.client_id,
                user_id=self.auth.user_id,
                agent_name=self.agent_name,
                app_code=self.auth.app_code,
                context_json=context_json,
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

                # Restore context from DB, merging with any in-memory values
                # (in-memory values from the current request take precedence)
                if session.context_json:
                    try:
                        db_context = json.loads(session.context_json)
                        self.context = {**db_context, **self.context}
                    except (json.JSONDecodeError, TypeError):
                        logger.warning(f"Invalid context_json for session {self.session_id}")

                # Restore app_code from context or DB if not provided in current request
                if self.auth and not self.auth.app_code:
                    restored_app_code = self.context.get("app_code") or session.app_code
                    if restored_app_code:
                        self.auth.app_code = restored_app_code

                # Rebuild conversation messages from persisted turn history
                await self._restore_conversation_history()
        except Exception as e:
            logger.warning(f"Failed to load session {self.session_id}: {e}")

    async def _restore_conversation_history(self) -> None:
        """Rebuild Anthropic-format messages from persisted turn summaries.

        Each turn in ai_session_history has user_instruction and assistant_summary.
        We reconstruct alternating user/assistant pairs so the LLM sees prior
        context on session resumption.
        """
        try:
            from app.services.context_manager import get_context_manager
            context_manager = get_context_manager()
            history, _ = await context_manager.get_history(self.session_id)

            if not history:
                return

            for turn in history:
                user_text = turn.user_instruction or ""
                assistant_text = turn.assistant_summary or "(Performed actions via tools)"

                if not user_text:
                    continue

                self.messages.append({
                    "role": "user",
                    "content": user_text,
                })
                self.messages.append({
                    "role": "assistant",
                    "content": [{"type": "text", "text": assistant_text}],
                })

            # Sync _turn_count to the actual max turn number persisted in DB,
            # so resumed sessions continue numbering correctly instead of restarting at 1.
            max_turn = max(h.turn_number for h in history)
            if max_turn > self._turn_count:
                self._turn_count = max_turn

            logger.info(
                f"Restored {len(history)} turns ({len(self.messages)} messages) "
                f"for session {self.session_id}, resuming from turn {self._turn_count}"
            )
        except Exception as e:
            logger.warning(f"Failed to restore conversation history: {e}")
