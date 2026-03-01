"""AppBuilderAgent — builds entire applications through conversation.

Extends BaseAgent with:
- AppBuilder-specific tools (pages, components, events, styles, entities)
- Component catalog integration (when available)
- API catalog integration (when available)
- System prompt from aicontext/ documentation
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.agent import BaseAgent
from app.core.session import BaseSession
from app.core.context import BaseContext
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
    ) -> None:
        """
        Args:
            context_builder: BaseContext loaded with aicontext docs.
            tools: List of ToolDefinitions. Defaults to empty (populated by registry).
            catalog: ComponentCatalog instance (optional).
            api_catalog: ApiCatalog instance (optional).
        """
        self._catalog = catalog
        self._catalog_context = catalog.to_prompt_context() if catalog else ""
        self._api_catalog = api_catalog
        self._api_catalog_context = api_catalog.to_prompt_context() if api_catalog else ""

        super().__init__(
            name="appbuilder",
            tools=tools or [],
            context_builder=context_builder,
            model_tier=settings.AGENT_MODEL_TIER,
            max_turns=settings.MAX_AGENT_TURNS,
            max_tokens=settings.AGENT_MAX_TOKENS,
        )

    def build_dynamic_context(self, session: BaseSession) -> str:
        """Build per-request dynamic context.

        Includes: auth info, component catalog, API catalog,
        and any session-specific state.
        """
        parts: list[str] = []

        if session.auth:
            app_code = session.context.get("app_code") or session.auth.app_code
            parts.append(
                f"Current session:\n"
                f"- Client: {session.auth.client_code}\n"
                f"- App: {app_code}\n"
            )

        if self._catalog_context:
            parts.append(self._catalog_context)

        if self._api_catalog_context:
            parts.append(self._api_catalog_context)

        return "\n\n".join(parts)

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        """Build context dict passed to each tool's execute function.

        Adds appbuilder-specific fields beyond the base context.
        """
        ctx = super().build_tool_context(session)
        if session.auth:
            ctx["app_code"] = session.context.get("app_code") or session.auth.app_code
            ctx["client_code"] = session.auth.client_code
        ctx["session_context"] = session.context
        return ctx
