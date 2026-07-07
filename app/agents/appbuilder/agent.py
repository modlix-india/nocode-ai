"""AppBuilderAgent — builds entire applications through conversation.

Extends BaseAgent with:
- AppBuilder-specific tools (pages, components, events, styles, entities)
- Component catalog integration (when available)
- API catalog integration (when available)
- Progressive tool documentation (group summary + per-turn details)
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.agent import BaseAgent
from app.core.session import BaseSession
from app.core.context import BaseContext
from app.agents.appbuilder.context import get_relevant_tool_details, extract_last_user_text
from app.config import settings

logger = logging.getLogger(__name__)


class AppBuilderAgent(BaseAgent):
    """Agent that builds no-code applications via tool-use."""

    # Mutating CRUD tools pause for user confirmation before executing. The
    # confirmation mechanism lives in BaseAgent; this set + the message body
    # below are AppBuilder-specific (page/component/app fields).
    CONFIRMATION_TOOLS: set[str] = {"create", "update", "delete", "copy"}

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
            context_builder: BaseContext with agent persona and tool groups summary.
            tools: List of ToolDefinitions. Defaults to empty (populated by registry).
            catalog: ComponentCatalog instance (optional).
            api_catalog: ApiCatalog instance (optional).
            provider: LLM provider name ("anthropic" or "openai"). Defaults to Anthropic.
        """
        self._catalog = catalog
        self._catalog_context = catalog.to_prompt_context() if catalog else ""
        self._api_catalog = api_catalog
        self._api_catalog_context = api_catalog.to_prompt_context() if api_catalog else ""

        from app.agents.appbuilder.tools.registry import TOOL_ROUTER

        super().__init__(
            name="appbuilder",
            tools=tools or [],
            context_builder=context_builder,
            model_tier=settings.AGENT_MODEL_TIER,
            max_turns=settings.MAX_AGENT_TURNS,
            max_tokens=settings.AGENT_MAX_TOKENS,
            provider=provider,
            router_tool=TOOL_ROUTER,
        )

    async def build_dynamic_context(self, session: BaseSession) -> str:
        """Build per-request dynamic context.

        Includes: auth info, relevant tool group details,
        component catalog, API catalog, and learned knowledge.
        """
        parts: list[str] = []

        if session.auth:
            app_code = session.context.get("app_code") or session.auth.app_code
            parts.append(
                f"Current session:\n"
                f"- Client: {session.auth.client_code}\n"
                f"- App: {app_code}\n"
            )

        # Progressive tool docs: inject detailed reference for relevant groups
        tool_details = get_relevant_tool_details(session.messages)
        if tool_details:
            parts.append(tool_details)

        if self._catalog_context:
            parts.append(self._catalog_context)

        if self._api_catalog_context:
            parts.append(self._api_catalog_context)

        # Learning loop: inject relevant knowledge from past sessions
        enhancement = await self._build_learning_enhancement(session)
        if enhancement:
            parts.append(enhancement)

        return "\n\n".join(parts)

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

        Adds appbuilder-specific fields beyond the base context.
        """
        ctx = super().build_tool_context(session)
        if session.auth:
            ctx["app_code"] = session.context.get("app_code") or session.auth.app_code
            ctx["client_code"] = session.auth.client_code
        ctx["session_context"] = session.context
        return ctx

    def _build_confirmation_message(
        self, tool_name: str, display_name: str, tool_input: dict[str, Any],
    ) -> str:
        """Build a human-readable confirmation message from tool input."""
        object_type = tool_input.get("object_type", "object")
        name = tool_input.get("name") or tool_input.get("page_name") or tool_input.get("id") or "?"
        message = tool_input.get("message", "")

        if tool_name == "create":
            return f"Create {object_type} '{name}'" + (f" — {message}" if message else "")
        if tool_name == "update":
            parts = []
            if tool_input.get("properties"):
                parts.append(f"properties: {list(tool_input['properties'].keys())}")
            if tool_input.get("operations"):
                ops = tool_input["operations"]
                op_summary = ", ".join(
                    f"{op.get('op', '?')} '{op.get('component_key', op.get('parent_key', '?'))}'"
                    for op in ops[:5]
                )
                if len(ops) > 5:
                    op_summary += f", +{len(ops) - 5} more"
                parts.append(f"component ops: [{op_summary}]")
            if tool_input.get("event_function"):
                fn_name = tool_input["event_function"].get("function_name", "?")
                parts.append(f"event function: {fn_name}")
            if tool_input.get("delete_event_function"):
                parts.append(f"delete event: {tool_input['delete_event_function']}")
            if tool_input.get("definition"):
                parts.append("definition update")
            detail = "; ".join(parts) if parts else message
            return f"Update {object_type} '{name}'" + (f" — {detail}" if detail else "")
        if tool_name == "delete":
            return f"Delete {object_type} '{name}'"
        if tool_name == "copy":
            src_name = tool_input.get("source_name", "?")
            src_app = tool_input.get("source_app_code", "?")
            tgt_app = tool_input.get("target_app_code", "?")
            tgt_name = tool_input.get("target_name") or src_name
            if tool_input.get("source_component_key"):
                return (
                    f"Copy subtree '{tool_input['source_component_key']}' from "
                    f"{object_type} '{src_name}' in app '{src_app}' into page "
                    f"'{tool_input.get('target_page_name', '?')}' in app '{tgt_app}'"
                )
            if object_type == "application":
                return f"Copy application '{src_app}' to new app '{tgt_app}'"
            return f"Copy {object_type} '{src_name}' from app '{src_app}' to app '{tgt_app}' as '{tgt_name}'"
        return f"{display_name} on {object_type} '{name}'"
