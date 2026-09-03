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


async def _resolve_app_user_id(
    client: Any,
    gateway_url: str,
    login_headers: dict[str, str],
    username: str,
    app_code: str,
) -> int:
    """Step 1 of app-user login: POST /findUserClients and pick the userId.

    Module-level so BaseSession._login_app_user stays under the linter's
    complexity bar. Returns the userId or raises with a clear hint.
    """
    find_resp = await client.post(
        f"{gateway_url}/api/security/users/findUserClients",
        headers=login_headers,
        json={"userName": username},
    )
    if find_resp.status_code >= 400:
        raise RuntimeError(
            f"findUserClients failed for app-user '{username}' in app "
            f"'{app_code}': HTTP {find_resp.status_code}: "
            f"{find_resp.text[:200]}"
        )
    rows = find_resp.json() if find_resp.content else []
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(
            f"No user '{username}' found in app '{app_code}'. "
            "Confirm the username and that this app has end users."
        )
    user_id = rows[0].get("userId")
    if not user_id:
        raise RuntimeError(
            f"findUserClients returned an entry without userId: {rows[0]}"
        )
    return user_id


# Session-context keys for app scope.
#
# `app_code` is the app the chat request opened with and never changes.
# FOCUS_APP_KEY is the app a write most recently landed in, and it wins: a
# session opened on appbuilder that goes on to build `crm` is working in `crm`,
# and everything scoped per-app (tool targets, pre-flight grounding, the KB,
# lore) has to agree about that. An agent that never sets it is unaffected.
FOCUS_APP_KEY = "focus_app_code"
SEEN_APPS_KEY = "written_app_codes"


def session_app_code(session: "BaseSession") -> str:
    """The app a session is working in: focus app, else the request app."""
    context = getattr(session, "context", None) or {}
    focus = context.get(FOCUS_APP_KEY) if isinstance(context, dict) else ""
    if isinstance(focus, str) and focus.strip():
        return focus.strip()
    request_app = context.get("app_code") if isinstance(context, dict) else ""
    auth = getattr(session, "auth", None)
    return request_app or (getattr(auth, "app_code", "") if auth else "") or ""


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
    path_prefix: str = ""  # Standalone mode: URL prefix e.g. /appbuilder/SYSTEM/page

    def to_headers(self) -> dict[str, str]:
        """Build HTTP headers for forwarding to Gateway APIs.

        Returns all 5 headers required by backend services
        (matches Java IFeignSecurityService Feign interface).
        The ``appCode`` header is set to the *access* app (appbuilder/sitezump),
        NOT the target app being built.
        """
        headers = {
            "Authorization": f"Bearer {self.token}" if not self.token.startswith("Bearer") else self.token,
            "X-Forwarded-Host": self.forwarded_host,
            "X-Forwarded-Port": self.forwarded_port,
            "clientCode": self.client_code,
            "appCode": self.access_app_code,
        }
        if self.path_prefix:
            headers["X-Path-Prefix"] = self.path_prefix
        return headers


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
        # What the model saw on the MOST RECENT call. Distinct from
        # total_usage["input_tokens"], which is the sum over every call in the
        # session — see get_usage_summary.
        self._last_context_tokens: int = 0
        self._turn_count: int = 0
        self._db_session_created: bool = False
        # App-user identity — separate from self.auth. Used only by tools that
        # interact with the CUSTOMER'S app as one of its end users
        # (screenshot_page, drive_page, call_as_app_user). Stored as the raw
        # request input (token or username+password) and resolved lazily by
        # get_app_user_token() into a cached bearer token for the conversation.
        self._app_user_input: Optional[dict[str, Any]] = None
        self._app_user_token: Optional[str] = None
        # Objects the caller has open and unsaved, as sent with this message. A
        # plain attribute rather than a context key on purpose: context is
        # persisted to CONTEXT_JSON and a page definition reaches 1.4MB. These are
        # per-message anyway, so there is nothing to carry forward.
        self.open_drafts: list[dict[str, Any]] = []

    def set_app_user(self, app_user: Optional[dict[str, Any]]) -> None:
        """Stash the app-user credentials from the ChatRequest.

        Pass either a dict with 'token' (pre-obtained app-user JWT) or 'username'
        + 'password'. None clears any prior value. Lazy: no auth call happens
        here — the token is resolved on first get_app_user_token().
        """
        self._app_user_input = app_user or None
        # If the caller passed a fresh credential set, drop any previously
        # cached token so the next get_app_user_token() re-resolves.
        if app_user:
            self._app_user_token = app_user.get("token") if app_user.get("token") else None

    async def get_app_user_token(self) -> str:
        """Resolve and return the app-user bearer token.

        Resolution order:
          1. Return cached token if one was provided or previously resolved.
          2. If username+password are set, run findUserClients + authenticate
             against `self.auth.app_code` (the target app being built) and
             cache the resulting accessToken.
          3. Raise RuntimeError with a clear remediation hint.
        """
        if self._app_user_token:
            return self._app_user_token

        username, password = self._require_app_user_creds()
        target_app, client_code = self._require_app_user_app()
        token = await self._login_app_user(username, password, target_app, client_code)
        self._app_user_token = token
        return token

    def _require_app_user_creds(self) -> tuple[str, str]:
        """Pull (username, password) from the app-user input or raise."""
        if not self._app_user_input:
            raise RuntimeError(
                "app-user credentials required for this tool. Pass "
                "`app_user.token` OR `app_user.{username, password}` in the "
                "chat request. The developer JWT (your Authorization header) "
                "doesn't have a session in the customer's app — it can only "
                "author platform objects, not render the app as an end user."
            )
        username = self._app_user_input.get("username")
        password = self._app_user_input.get("password")
        if not (username and password):
            raise RuntimeError(
                "app_user provided but missing username or password. Pass "
                "both, or pass an already-obtained `token` directly."
            )
        return username, password

    def _require_app_user_app(self) -> tuple[str, str]:
        """Pull (app_code, client_code) for app-user login or raise."""
        if not self.auth or not self.auth.app_code:
            raise RuntimeError(
                "app-user login needs a target app_code on the session — "
                "set `app_code` on the chat request before invoking tools "
                "that need an app-user identity."
            )
        return self.auth.app_code, self.auth.client_code or ""

    async def _login_app_user(
        self, username: str, password: str, app_code: str, client_code: str,
    ) -> str:
        """Run findUserClients + authenticate, return the accessToken.

        Two-step platform auth kept inline (not via the modlix port) to avoid
        a cyclic dependency between core/ and agents/.
        """
        import httpx
        from app.config import settings

        gw = settings.GATEWAY_URL.rstrip("/")
        login_headers = {"appCode": app_code, "clientCode": client_code}
        async with httpx.AsyncClient(timeout=getattr(settings, "HTTP_TIMEOUT", 30.0)) as client:
            user_id = await _resolve_app_user_id(
                client, gw, login_headers, username, app_code,
            )
            auth_resp = await client.post(
                f"{gw}/api/security/authenticate",
                headers=login_headers,
                json={
                    "userName": username,
                    "userId": user_id,
                    "password": password,
                    "rememberMe": False,  # app-user sessions stay short-lived
                },
            )
            if auth_resp.status_code >= 400:
                raise RuntimeError(
                    f"authenticate failed for app-user '{username}' (userId={user_id}) "
                    f"in app '{app_code}': HTTP {auth_resp.status_code}: "
                    f"{auth_resp.text[:200]}"
                )
            body = auth_resp.json() if auth_resp.content else {}
            token = body.get("accessToken") if isinstance(body, dict) else None
            if not token:
                raise RuntimeError(
                    f"authenticate response missing accessToken for "
                    f"app-user '{username}': {body}"
                )
            return token

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

    def append_assistant_message(
        self,
        content_blocks: list[dict[str, Any]],
        reasoning_content: str | None = None,
    ) -> None:
        """Append an assistant message (may contain text + tool_use blocks).

        Args:
            content_blocks: Anthropic-style content blocks.
            reasoning_content: Optional CoT reasoning from thinking-mode
                providers (e.g. DeepSeek). Stored as ``_reasoning_content``
                so providers can pass it back on subsequent turns.
        """
        msg: dict[str, Any] = {
            "role": "assistant",
            "content": content_blocks,
        }
        if reasoning_content:
            msg["_reasoning_content"] = reasoning_content
        self.messages.append(msg)

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

    _ELIDED_FLAG = "_elided"

    @staticmethod
    def _content_chars(content: Any) -> int:
        """Rough char weight of one message's content, images included.

        Images are counted by their base64 length because that is what actually
        travels; a screenshot dwarfs any text block in the same result.
        """
        if isinstance(content, str):
            return len(content)
        if not isinstance(content, list):
            return 0
        total = 0
        for block in content:
            if isinstance(block, str):
                total += len(block)
            elif isinstance(block, dict):
                total += len(block.get("text") or "")
                inner = block.get("content")
                if inner is not None and inner is not block:
                    total += BaseSession._content_chars(inner)
                src = block.get("source")
                if isinstance(src, dict):
                    total += len(src.get("data") or "")
        return total

    def history_chars(self) -> int:
        """Total char weight of the conversation. Cheap stand-in for tokens."""
        return sum(self._content_chars(m.get("content")) for m in self.messages
                   if isinstance(m, dict))

    @staticmethod
    def _is_image(block: Any) -> bool:
        return isinstance(block, dict) and block.get("type") in ("image", "image_url")

    def _drop_old_images(self, keep_turns: int) -> int:
        """Replace screenshots outside the recent window with a text note.

        Images dominate history weight — a single screenshot is 100-500KB of
        base64, re-sent on every subsequent turn — and they need a far shorter
        window than text: the model looked at the shot when it arrived and wrote
        down what it saw, so the pixels stop earning their place almost at once.

        The newest image is always kept even when `keep_turns` would drop it, so
        the screenshot -> patch -> screenshot -> compare loop always has the shot
        it just took.
        """
        positions = [i for i, m in enumerate(self.messages)
                     if isinstance(m, dict) and m.get("role") == "assistant"]
        if len(positions) <= keep_turns:
            return 0
        cutoff = positions[-keep_turns]

        # Find the newest image anywhere, so it can be spared.
        newest: tuple[int, int, int] | None = None
        for mi, msg in enumerate(self.messages):
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for bi, block in enumerate(content):
                if self._is_image(block):
                    newest = (mi, bi, -1)
                elif isinstance(block, dict) and isinstance(block.get("content"), list):
                    for ii, inner in enumerate(block["content"]):
                        if self._is_image(inner):
                            newest = (mi, bi, ii)

        freed = 0

        def _swap(container: list, idx: int, at: tuple) -> int:
            if newest is not None and at == newest:
                return 0
            weight = self._content_chars([container[idx]])
            container[idx] = {"type": "text",
                              "text": "[screenshot dropped from history — "
                                      "take a fresh one if you need to look again]"}
            return weight

        for mi, msg in enumerate(self.messages[:cutoff]):
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for bi, block in enumerate(list(content)):
                if self._is_image(block):
                    freed += _swap(content, bi, (mi, bi, -1))
                elif isinstance(block, dict) and isinstance(block.get("content"), list):
                    inner_list = block["content"]
                    for ii, inner in enumerate(list(inner_list)):
                        if self._is_image(inner):
                            freed += _swap(inner_list, ii, (mi, bi, ii))
        return freed

    def elide_old_tool_results(
        self,
        keep_recent_turns: int = 6,
        over_chars: int = 200_000,
        min_result_chars: int = 1500,
        keep_images_turns: int = 3,
    ) -> int:
        """Shrink old tool_result payloads once history gets big. Returns chars freed.

        Nothing happens below `over_chars`, so short conversations are untouched.
        Above it, `tool_result` blocks older than the last `keep_recent_turns`
        assistant turns have their content replaced by a stub that keeps a
        200-char head of the original, and any image they carried is dropped.

        Deliberately narrow:
        - The block is REPLACED, never removed, because every `tool_use` needs a
          matching `tool_result` or the next request is rejected.
        - User messages and assistant text/reasoning are never touched: they are
          small and they carry the plan.
        - Results under `min_result_chars` are left alone. They are cheap and
          usually the ones holding ids and keys the model still needs.
        - Already-elided blocks are flagged so repeat passes are free.

        The cost of getting this wrong is a re-fetch (one turn), not a wrong
        answer, which is why the recent window is kept whole.
        """
        if over_chars <= 0 or self.history_chars() <= over_chars:
            return 0

        assistant_positions = [i for i, m in enumerate(self.messages)
                               if isinstance(m, dict) and m.get("role") == "assistant"]
        freed = 0
        if len(assistant_positions) <= keep_recent_turns:
            # Too few turns for the TEXT window to have anything behind it. The
            # image pass still runs: its window is much shorter, and a short
            # conversation carrying several screenshots is exactly the case that
            # blew up before (721,910 chars of history, 5,405 reclaimed).
            freed += self._drop_old_images(keep_images_turns)
            if freed:
                logger.info("Elided %d chars of history (now %d)", freed, self.history_chars())
            return freed
        # Everything at or after this index belongs to the recent window.
        cutoff = assistant_positions[-keep_recent_turns]

        for msg in self.messages[:cutoff]:
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if (not isinstance(block, dict)
                        or block.get("type") != "tool_result"
                        or block.get(self._ELIDED_FLAG)):
                    continue
                before = self._content_chars(block.get("content"))
                if before < min_result_chars:
                    continue
                head = self._result_head(block.get("content"))
                block["content"] = (
                    f"{head}\n[… {before} chars elided from this earlier result to "
                    f"keep the conversation inside the context window. Re-run the "
                    f"tool if you need the rest.]"
                )
                block[self._ELIDED_FLAG] = True
                freed += before - self._content_chars(block["content"])
        freed += self._drop_old_images(keep_images_turns)
        if freed:
            logger.info("Elided %d chars of history (now %d)", freed, self.history_chars())
        return freed

    @staticmethod
    def _result_head(content: Any, limit: int = 200) -> str:
        """First `limit` chars of a result's text, for the stub. Images yield ''."""
        if isinstance(content, str):
            return content[:limit]
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return (block.get("text") or "")[:limit]
                if isinstance(block, str):
                    return block[:limit]
        return ""

    def accumulate_usage(self, usage: dict[str, Any]) -> None:
        """Add token usage from one LLM call to running totals."""
        for key in self.total_usage:
            self.total_usage[key] += usage.get(key, 0)
        # Overwrite, never add: this call's input IS the conversation size.
        #
        # The cache_read term is required, not optional: every provider reports
        # input_tokens EXCLUDING cached reads (Anthropic natively; DeepSeek via
        # _openai_compatible_usage, which splits prompt_tokens into
        # miss -> input and hit -> cache_read). Cached tokens are still tokens
        # the model read, so they count toward context occupancy.
        self._last_context_tokens = (
            usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
        )

    def get_usage_summary(self) -> dict[str, Any]:
        """Return a compact usage summary for the client.

        Includes total_tokens, context_percent, and turns — the fields
        the UI needs to display usage indicators.
        """
        input_t = self.total_usage["input_tokens"]
        output_t = self.total_usage["output_tokens"]
        cache_read_t = self.total_usage["cache_read_input_tokens"]

        # Context used is the size of the CURRENT conversation — the input of
        # the most recent LLM call — not the sum of every call's input.
        #
        # The agent loop makes one LLM call per tool round-trip (up to
        # max_turns), and each call re-sends the whole conversation. Summing
        # their inputs therefore measures cumulative spend, not occupancy: a
        # 26-iteration run showed 1.46M cumulative against a real context of
        # 64K, so the bar pinned at 100% while the window was 6% full. Raising
        # CONTEXT_LIMIT_DEFAULT (48K -> 112K, and now 1M) only delayed the
        # pin, because the cumulative number grows without bound.
        context_used = self._last_context_tokens
        from app.config import settings
        context_limit = settings.CONTEXT_LIMIT_DEFAULT
        context_percent = round(context_used / context_limit * 100, 1) if context_limit > 0 else 0

        return {
            "input_tokens": input_t,
            "output_tokens": output_t,
            # Cached reads are billable tokens the model processed, so they
            # belong in the total. Including them also keeps this number stable
            # now that DeepSeek splits prompt_tokens into input + cache_read —
            # without it, switching cache reporting on would have made the
            # displayed total collapse overnight for no real reason.
            "total_tokens": input_t + cache_read_t + output_t,
            "cache_read_tokens": cache_read_t,
            "context_used": context_used,
            "context_limit": context_limit,
            "context_percent": min(context_percent, 100.0),
            "turns": self._turn_count,
        }

    def start_turn(self) -> None:
        """Increment the turn counter at the beginning of a new turn.

        Must be called once at the start of each agent turn (before the
        LLM loop), so that ``persist_turn_incremental`` and ``persist_turn``
        both use the same ``_turn_count`` and write to the same DB row.
        """
        self._turn_count += 1
        self._turn_started = True

    async def persist_turn(
        self,
        user_text: str,
        assistant_summary: str,
        tool_calls: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> None:
        """Persist a conversation turn to the database for analytics and training.

        Args:
            user_text: The user's message for this turn.
            assistant_summary: Text summary of the assistant's response.
            tool_calls: List of tool call records for this turn. Each entry:
                {"tool": str, "input": dict, "success": bool, "summary": str}
            model: LLM model name used for this turn.

        This is a best-effort operation — failures are logged but don't
        stop the agent.

        Uses upsert so that if persist_turn_incremental() already created
        the row during the tool loop, the final complete data overwrites it.
        """
        if not self.auth:
            return

        # If start_turn() was not called (error before the agent loop),
        # increment now so we still get a valid turn_number.
        if not getattr(self, '_turn_started', False):
            self._turn_count += 1
        self._turn_started = False

        tool_calls_json: str | None = None
        if tool_calls:
            tool_calls_json = json.dumps(tool_calls)

        try:
            from app.services.context_manager import get_context_manager
            context_manager = get_context_manager()
            request_id = uuid.uuid4().hex[:8]
            # Use upsert (not plain insert) because persist_turn_incremental()
            # may have already created this turn during the tool-use loop.
            await context_manager.upsert_turn(
                session_id=self.session_id,
                request_id=request_id,
                turn_number=self._turn_count,
                user_instruction=user_text,
                assistant_summary=assistant_summary,
                tool_calls_json=tool_calls_json,
                model=model,
            )
        except Exception as e:
            logger.warning(f"Failed to persist turn: {e}")

        # Update TURN_COUNT in the session table so the UI shows
        # the correct turn count on refresh.
        try:
            from app.services.session_manager import get_session_manager
            session_manager = get_session_manager()
            await session_manager.increment_turn_count(
                self.session_id, self.auth.user_id if self.auth else None
            )
        except Exception as e:
            logger.warning(f"Failed to update turn count: {e}")

    async def record_token_usage(
        self,
        usage: dict[str, Any],
        request_id: str,
        model: str,
        provider_name: str | None = None,
    ) -> None:
        """Record token usage for a single LLM call.

        Args:
            usage: Token usage dict from LLM response.
            request_id: Unique request identifier.
            model: Model name used for this call.
            provider_name: LLM provider name (e.g. "anthropic", "deepseek").
                Falls back to settings.LLM_PROVIDER if not provided.

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
                llm_provider=provider_name or settings.LLM_PROVIDER,
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
        model: str | None = None,
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
                turn_number=self._turn_count,
                user_instruction=user_text,
                assistant_summary=assistant_summary,
                tool_calls_json=tool_calls_json,
                model=model,
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
            context_json = self._serialize_context(self.context)
            await get_session_manager().update_session_context(
                self.session_id, context_json, self.auth.user_id if self.auth else None
            )
        except Exception as e:
            logger.warning("Failed to save session context: %s (keys=%s)",
                           e, list(self.context.keys()) if self.context else "none")

    # ── Internal helpers ────────────────────────────────────────

    # Transient runtime keys that live for one turn only — never persisted.
    # _started_tuids is a *set* (opened sub-agent card ids) JSON can't serialize;
    # left in, it sinks the whole context save and the conversation loses its
    # memory next message.
    _EPHEMERAL_CONTEXT_KEYS = {"_started_tuids"}

    @staticmethod
    def _serialize_context(context: dict) -> str:
        """JSON-encode session context for persistence. Drops ephemeral runtime
        keys, and degrades any stray non-JSON value (e.g. a set) to a list/str so
        one bad value can never again sink the entire context."""
        persistable = {k: v for k, v in context.items()
                       if k not in BaseSession._EPHEMERAL_CONTEXT_KEYS}
        return json.dumps(persistable, default=lambda o: list(o) if isinstance(o, set) else str(o))

    async def _create_new_session(self) -> None:
        """Create a new session in the database."""
        try:
            from app.services.session_manager import get_session_manager
            session_manager = get_session_manager()
            context_json = self._serialize_context(self.context) if self.context else None
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

    def _clear_focus_on_app_switch(self, prior_request_app: str | None) -> None:
        """Drop a persisted focus app when the user has navigated to another app.

        The focus (see FOCUS_APP_KEY) outranks the request's `app_code`, which is
        what makes a session follow the app it is building. That must not outlive
        the user opening a different app in the workspace: their explicit
        selection beats an inference drawn from earlier writes.

        The test is a CHANGE in the request app between turns, not a difference
        between the request app and the focus. Asking a follow-up from the same
        place sends the same `app_code` as before and means nothing new, so the
        focus survives — otherwise turn two of "build me a CRM" would snap
        straight back to `appbuilder` and undo the fix.
        """
        incoming = self.context.get("app_code")
        if not incoming or not prior_request_app or incoming == prior_request_app:
            return
        dropped = self.context.pop(FOCUS_APP_KEY, None)
        # The grounding block names an app, so it goes with the focus.
        self.context.pop("_preflight_grounding", None)
        self.context.pop("_preflight_grounding_app", None)
        if dropped:
            logger.info(
                "session %s: request app %s -> %s, dropping focus '%s'",
                self.session_id, prior_request_app, incoming, dropped,
            )

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
                        prior_request_app = db_context.get("app_code")
                        self.context = {**db_context, **self.context}
                        self._clear_focus_on_app_switch(prior_request_app)
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
                assistant_text = (
                    turn.assistant_summary
                    or _tool_only_turn_note(turn.tool_calls_json)
                )

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


def _tool_only_turn_note(tool_calls_json: str | None) -> str:
    """Stand-in assistant text for a restored turn that produced no prose.

    Built from the tools' own result summaries so it reads like a NORMAL
    reply. Any meta-placeholder in this slot eventually gets parroted
    verbatim into chat by the resumed model - both "(Performed actions via
    tools)" and a bracketed "[transcript note: ...]" were, live - so the
    only safe stand-in is text that is also acceptable user-facing prose.
    Elicitation turns (widget was the reply) restore as the widget's own
    summary ("Map + prompt shown for ..."), which is exactly the context
    the resumed model needs."""
    calls: list[dict] = []
    if tool_calls_json:
        try:
            calls = [c for c in json.loads(tool_calls_json) if isinstance(c, dict)]
        except (ValueError, TypeError):
            pass
    summaries = [s for s in ((c.get("summary") or "").strip() for c in calls) if s]
    if summaries:
        return " ".join(summaries)[:500]
    tool_names = list(dict.fromkeys(c.get("tool") for c in calls if c.get("tool")))
    if tool_names:
        return f"Done ({', '.join(tool_names)})."
    return "Done."
