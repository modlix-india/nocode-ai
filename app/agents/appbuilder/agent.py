"""AppBuilderAgent — builds entire applications through conversation.

Extends BaseAgent with:
- AppBuilder-specific tools (pages, components, events, styles, entities)
- Deferred tool loading (core tools in prompt, rest discovered via ToolSearch)
- Component catalog integration (when available)
- API catalog integration (when available)
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.agent import BaseAgent
from app.core.session import BaseSession
from app.core.context import BaseContext
from app.agents.appbuilder.context import extract_last_user_text
from app.agents.appbuilder.tools.definition_cache import DefinitionCache
from app.agents.appbuilder.tools.result_store import ResultStore
from app.config import settings

logger = logging.getLogger(__name__)


class AppBuilderAgent(BaseAgent):
    """Agent that builds no-code applications via tool-use."""

    def __init__(
        self,
        context_builder: BaseContext,
        tools: list | None = None,
        catalog: Any = None,
        api_catalog: Any = None,
        provider: str = "anthropic",
    ) -> None:
        """
        Args:
            context_builder: BaseContext with agent persona.
            tools: List of ToolDefinitions. Defaults to registry CORE_TOOLS.
            catalog: ComponentCatalog instance (optional).
            api_catalog: ApiCatalog instance (optional).
            provider: LLM provider name ("anthropic" or "openai"). Defaults to Anthropic.
        """
        self._catalog = catalog
        self._catalog_context = catalog.to_prompt_context() if catalog else ""
        self._api_catalog = api_catalog
        self._api_catalog_context = api_catalog.to_prompt_context() if api_catalog else ""

        from app.agents.appbuilder.tools.registry import CORE_TOOLS, DEFERRED_TOOLS

        # Per-session caches (created lazily per session in build_tool_context)
        self._session_caches: dict[str, DefinitionCache] = {}
        self._session_result_stores: dict[str, ResultStore] = {}

        super().__init__(
            name="appbuilder",
            tools=tools or CORE_TOOLS,
            context_builder=context_builder,
            model_tier=settings.AGENT_MODEL_TIER,
            max_turns=settings.MAX_AGENT_TURNS,
            max_tokens=settings.AGENT_MAX_TOKENS,
            provider=provider,
            deferred_tools=DEFERRED_TOOLS,
        )

    async def build_dynamic_context(self, session: BaseSession) -> str:
        """Build per-request dynamic context.

        Includes: auth info, component catalog, API catalog, learned knowledge,
        and auto-scraped website data when the user provides a URL.
        """
        parts: list[str] = []

        if session.auth:
            app_code = session.context.get("app_code") or session.auth.app_code
            host = session.auth.forwarded_host or "localhost"
            scheme = "https" if host != "localhost" else "http"
            port_suffix = f":{session.auth.forwarded_port}" if session.auth.forwarded_port not in ("443", "80", "") else ""
            base_url = f"{scheme}://{host}{port_suffix}"
            parts.append(
                f"Current session:\n"
                f"- Client: {session.auth.client_code}\n"
                f"- App: {app_code}\n"
                f"- Preview URL pattern: {base_url}/<appCode>/{session.auth.client_code}/page/<pageName>\n"
            )

        # Auto-scrape URLs in the latest user message
        scraped = await self._auto_scrape_urls(session)
        if scraped:
            parts.append(scraped)

        if self._catalog_context:
            parts.append(self._catalog_context)

        if self._api_catalog_context:
            parts.append(self._api_catalog_context)

        # Learning loop: inject relevant knowledge from past sessions
        enhancement = await self._build_learning_enhancement(session)
        if enhancement:
            parts.append(enhancement)

        return "\n\n".join(parts)

    async def _auto_scrape_urls(self, session: BaseSession) -> str:
        """Detect URLs in the latest user message and scrape them.

        Returns formatted scraped data or empty string.
        """
        from app.agents.appbuilder.tools.web_scraper import (
            extract_urls_from_text,
            scrape_website,
            format_scraped_data_for_agent,
        )

        last_user_text = extract_last_user_text(session.messages)
        if not last_user_text:
            return ""

        urls = extract_urls_from_text(last_user_text)
        if not urls:
            return ""

        # Only scrape the first URL to avoid excessive latency
        url = urls[0]
        logger.info("Auto-scraping URL from user message: %s", url)

        try:
            data = await scrape_website(url)
            if data.get("error"):
                logger.warning("Scrape failed: %s", data["error"])
                return f"[Attempted to scrape {url} but failed: {data['error']}]"

            formatted = format_scraped_data_for_agent(data)

            # Store screenshot in session context for vision-capable models
            if data.get("screenshot_base64"):
                session.context["scraped_screenshot"] = data["screenshot_base64"]
                session.context["scraped_url"] = url
                logger.info("Screenshot captured for %s", url)

            logger.info("Scraped %s: %d sections, %d colors, %d images",
                       url,
                       len(data.get("sections", [])),
                       len(data.get("colors", [])),
                       len(data.get("images", [])))
            return formatted

        except Exception as e:
            logger.warning("Auto-scrape failed for %s: %s", url, e)
            return f"[Attempted to scrape {url} but failed: {e}]"

    async def _build_learning_enhancement(self, session: BaseSession) -> str:
        """Retrieve and format learned knowledge for prompt injection."""
        try:
            from app.learning.prompt_enhancer import get_prompt_enhancer

            last_user_msg = extract_last_user_text(session.messages)
            if not last_user_msg:
                return ""

            return await get_prompt_enhancer().build_enhancement(
                agent_name=self.name,
                user_message=last_user_msg,
                session_context=session.context,
            )
        except Exception as e:
            logger.debug("Prompt enhancement skipped: %s", e)
            return ""

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        """Build context dict passed to each tool's execute function.

        Adds appbuilder-specific fields beyond the base context:
        - definition_cache: LRU cache for definition metadata
        - result_store: persistence for oversized tool results
        - catalog: component catalog for validation
        """
        ctx = super().build_tool_context(session)
        if session.auth:
            ctx["app_code"] = session.context.get("app_code") or session.auth.app_code
            ctx["client_code"] = session.auth.client_code
        ctx["session_context"] = session.context

        # Lazily create per-session caches
        sid = session.session_id
        if sid not in self._session_caches:
            self._session_caches[sid] = DefinitionCache()
        if sid not in self._session_result_stores:
            self._session_result_stores[sid] = ResultStore()

        ctx["definition_cache"] = self._session_caches[sid]
        ctx["result_store"] = self._session_result_stores[sid]

        if self._catalog:
            ctx["catalog"] = self._catalog

        # Orchestrator context for delegate_task tool
        if hasattr(self, '_current_provider'):
            ctx["_session"] = session
            ctx["_provider"] = self._current_provider
            ctx["_event_stream"] = getattr(self, '_current_event_stream', None)
            ctx["_all_tools"] = self.tools
            ctx["_context_builder"] = self.context_builder
            ctx["_tool_context_builder"] = self.build_tool_context

        return ctx
