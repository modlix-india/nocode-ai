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

    async def build_dynamic_context(self, session: BaseSession, user_message: str = "") -> str:
        """Build per-request dynamic context.

        Includes: auth info, component catalog, API catalog, learned knowledge,
        and auto-scraped website data when the user provides a URL.
        """
        parts: list[str] = []

        if session.auth:
            app_code = session.context.get("app_code") or session.auth.app_code
            # X-Forwarded-Host may be comma-separated (e.g. "apps.local.modlix.com,localhost:8080");
            # take just the first (primary) host so the preview URL is valid.
            raw_host = session.auth.forwarded_host or "localhost"
            host = raw_host.split(",")[0].strip()
            scheme = "https" if host not in ("localhost", "127.0.0.1") and "localhost" not in host else "http"
            # Strip port from host if embedded (e.g. "localhost:8080")
            if ":" in host:
                host_part, port_part = host.rsplit(":", 1)
                port_suffix = f":{port_part}" if port_part not in ("443", "80") else ""
                host = host_part
            else:
                port_suffix = f":{session.auth.forwarded_port}" if session.auth.forwarded_port not in ("443", "80", "") else ""
            base_url = f"{scheme}://{host}{port_suffix}"
            parts.append(
                f"Current session:\n"
                f"- Client: {session.auth.client_code}\n"
                f"- App: {app_code}\n"
                f"- Preview URL pattern: {base_url}/<appCode>/{session.auth.client_code}/page/<pageName>\n"
            )

        # Auto-scrape URLs in the current user message
        scraped = await self._auto_scrape_urls(session, user_message)
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

    async def _auto_scrape_urls(self, session: BaseSession, user_message: str = "") -> str:
        """Detect URLs in the current user message and scrape them.

        Args:
            session: Active session.
            user_message: The current user message (not yet in session.messages).

        Returns formatted scraped data or empty string.
        """
        from app.agents.appbuilder.tools.web_scraper import (
            extract_urls_from_text,
            scrape_website,
            format_scraped_data_with_styles,
        )

        # Check current message first, then fall back to last message in history
        text_to_check = user_message or extract_last_user_text(session.messages)
        if not text_to_check:
            return ""

        urls = extract_urls_from_text(text_to_check)
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

            logger.info("Scraped %s: %d sections, %d colors, %d images",
                       url,
                       len(data.get("sections", [])),
                       len(data.get("colors", [])),
                       len(data.get("images", [])))

            # If we have a screenshot, analyze it with Claude Haiku vision
            # to extract the actual visual design (colors, fonts, layout)
            screenshot = data.get("screenshot_base64")
            if screenshot:
                session.context["scraped_screenshot"] = screenshot
                session.context["scraped_url"] = url

                from app.agents.appbuilder.tools.style_analyzer import analyze_and_format_styles
                try:
                    logger.info("Analyzing screenshot styles with Claude Haiku vision...")
                    design_brief = await analyze_and_format_styles(screenshot, data)
                    logger.info("Style analysis complete: %d chars", len(design_brief))
                    # Return the vision-enhanced design brief instead of basic scrape
                    return design_brief
                except Exception as e:
                    logger.warning("Style analysis failed, falling back to basic scrape: %s", e)

            # Fallback: scraper output with pre-converted Modlix styles
            return format_scraped_data_with_styles(data)

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
