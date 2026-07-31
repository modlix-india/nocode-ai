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
from app.agents.appbuilder.context import (
    HOT_TOOLS,
    extract_last_user_text,
    get_relevant_tool_details,
)
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

        # Deferred-schema surface (Phase 3): the LLM sees each tool's name +
        # one-liner description with empty parameters in the API `tools=` field,
        # and pulls full schemas on demand via `get_tool_schema`. The system
        # prompt's tool catalog (see TOOL_GROUPS_SUMMARY in
        # app.agents.appbuilder.context) lists every advertised tool by group.
        # The legacy tool-of-tools router (TOOL_ROUTER) is retired from this
        # agent — kept exported by the registry for other potential callers but
        # not wired here.
        super().__init__(
            name="appbuilder",
            tools=tools or [],
            context_builder=context_builder,
            model_tier=settings.AGENT_MODEL_TIER,
            max_turns=settings.MAX_AGENT_TURNS,
            max_tokens=settings.AGENT_MAX_TOKENS,
            provider=provider,
            defer_schemas=True,
        )

    def _tool_to_advertised_schema(self, tool: Any) -> dict[str, Any]:
        """Override BaseAgent's deferred-schema renderer for hot tools.

        Tools in `HOT_TOOLS` ship with their FULL Anthropic-shape schema in the
        tools[] payload (not the stripped {"type":"object","properties":{}} the
        deferred pattern uses for the long tail). Reason: the synthetic-retry
        round-trip on first-time calls was costing 1 extra LLM turn per unique
        tool used in a conversation. For multi-write tasks touching 5-7 unique
        tools, that's 5-7 wasted turns per conv.

        Trade-off: ~3-5K extra tokens in the system-prompt prefix per session.
        DeepSeek's automatic prefix caching makes that a one-time cost.

        For tools NOT in HOT_TOOLS, defer to BaseAgent's stripped form — the
        long-tail tools still go through search_tools / get_tool_schema.
        """
        if tool.name in HOT_TOOLS:
            return tool.to_anthropic_tool()
        return super()._tool_to_advertised_schema(tool)

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        """Extend BaseAgent's context with appbuilder-specific fields.

        ALSO pre-marks every HOT_TOOLS member in `fetched_schemas` so the
        dispatch gate at `_gate_deferred_dispatch` passes on first call —
        matching the full-schema advertisement above. The schema is already
        in the LLM's tools[] payload, so a synthetic retry would be pure
        overhead.
        """
        ctx = super().build_tool_context(session)
        fetched = ctx.get("fetched_schemas")
        if isinstance(fetched, list):
            for name in HOT_TOOLS:
                if name not in fetched:
                    fetched.append(name)
        elif isinstance(fetched, set):
            fetched.update(HOT_TOOLS)
        if session.auth:
            ctx["app_code"] = session.context.get("app_code") or session.auth.app_code
            ctx["client_code"] = session.auth.client_code
        ctx["session_context"] = session.context
        return ctx

    async def build_dynamic_context(self, session: BaseSession) -> str:
        """Build per-request dynamic context.

        Includes: auth info, pre-flight app grounding, relevant tool group
        details, component catalog, API catalog, and learned knowledge.
        """
        parts: list[str] = []

        if session.auth:
            app_code = session.context.get("app_code") or session.auth.app_code
            parts.append(
                f"Current session:\n"
                f"- Client: {session.auth.client_code}\n"
                f"- App: {app_code}\n"
            )

        # Pre-flight grounding: fetch app definition + top pages once per
        # session so the agent walks in knowing the structure. Saves 3-10
        # "list_pages" / "get_app" round-trips on most conversations.
        grounding = await self._build_preflight_grounding(session)
        if grounding:
            parts.append(grounding)

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

    _NAMED_PAGE_REF_KEYS: tuple[str, ...] = (
        "defaultPage", "loginPage", "shellPage", "forbiddenPage",
        "notFoundPage", "signUp", "forgotPasswordPage",
        "termsConditionPage", "privacyPolicyPage",
    )

    @staticmethod
    def _extract_named_page_refs(props: dict[str, Any]) -> list[tuple[str, str]]:
        """Pull (key, pageName) pairs from app properties. Handles both
        string values and ComponentProperty-shape `{"value": "..."}` dicts."""
        out: list[tuple[str, str]] = []
        for key in AppBuilderAgent._NAMED_PAGE_REF_KEYS:
            v = props.get(key)
            if isinstance(v, str) and v:
                out.append((key, v))
                continue
            if isinstance(v, dict):
                inner = v.get("value")
                if isinstance(inner, str) and inner:
                    out.append((key, inner))
        return out

    @staticmethod
    def _format_grounding(app_code: str, app_obj: dict | None, page_names: list[str]) -> str:
        """Render the fetched grounding into a Markdown section."""
        lines = [f"## Pre-flight grounding for app `{app_code}`",
                 "(fetched once at session start — use directly; don't re-fetch)"]
        if app_obj:
            page_refs = AppBuilderAgent._extract_named_page_refs(app_obj.get("properties") or {})
            if page_refs:
                lines.append("**Named page references (from application properties):**")
                lines.extend(f"- `{key}` → page `{name}`" for key, name in page_refs)
        if page_names:
            shown = page_names[:25]
            lines.append(f"**Pages in app ({len(page_names)} total, first {len(shown)}):**")
            lines.append("  " + ", ".join(f"`{n}`" for n in shown))
            if len(page_names) > len(shown):
                lines.append(f"  …and {len(page_names) - len(shown)} more. Use `list_pages` if you need them.")
        lines.append("")
        lines.append("Use these names directly. Don't call `list_pages` / `get_app` "
                     "again unless you need page contents (use `get_page`/`get_page_summary` for those).")
        return "\n".join(lines)

    @staticmethod
    def _first_app_from_response(resp: Any) -> dict | None:
        if not getattr(resp, "success", False) or not resp.data:
            return None
        content = (resp.data or {}).get("content") or []
        return content[0] if content else None

    @staticmethod
    def _page_names_from_response(resp: Any) -> list[str]:
        if not getattr(resp, "success", False) or not resp.data:
            return []
        return [
            p.get("name") for p in (resp.data or {}).get("content") or []
            if isinstance(p.get("name"), str)
        ]

    async def _fetch_grounding(self, session: BaseSession, app_code: str) -> tuple[dict | None, list[str]]:
        """Issue the two gateway calls and parse their responses.

        Uses the same singleton SaasClient + auth headers that every tool uses
        (`get_saas_client()` + `AuthContext.to_headers()`), so the round-trip
        looks identical to a tool call on the wire.

        Returns (app_obj_or_None, page_names). Empty/failed responses come
        back as None / []; the caller handles the "nothing to inject" case.
        """
        try:
            from app.agents.appbuilder.tools._shared import get_saas_client
            client = get_saas_client()
            headers = session.auth.to_headers()
            app_r = await client.get("/api/ui/applications", headers=headers,
                                     params={"appCode": app_code})
            pages_r = await client.get("/api/ui/pages", headers=headers,
                                       params={"appCode": app_code, "page": 0, "size": 30})
        except Exception as e:  # noqa: BLE001
            logger.debug("Pre-flight grounding fetch failed (%s: %s)", type(e).__name__, e)
            return None, []

        return self._first_app_from_response(app_r), self._page_names_from_response(pages_r)

    async def _build_preflight_grounding(self, session: BaseSession) -> str:
        """Fetch app definition + top page names once per session, cache on
        session.context, and format as a system-prompt section.

        Why: every new conversation otherwise starts with the agent calling
        `get_app` + `list_pages` + `search_page_components` (3-10 round-trips)
        before doing any actual work. Injecting this context up-front skips
        that entire pre-flight phase. Cached because it doesn't change within
        a conversation (an app's structure is stable; page CRUD invalidates
        but we accept the staleness for now — agent re-reads only when its
        action requires fresh data).

        Failure is silent — if the gateway is down or the app doesn't exist,
        we omit the section rather than blocking the conversation. The agent
        will fall back to its previous behaviour (call get_app itself).
        """
        if not session.auth:
            return ""
        app_code = session.context.get("app_code") or session.auth.app_code
        if not app_code:
            return ""
        cached = session.context.get("_preflight_grounding")
        if isinstance(cached, str):
            return cached

        app_obj, page_names = await self._fetch_grounding(session, app_code)
        if not app_obj and not page_names:
            session.context["_preflight_grounding"] = ""
            return ""
        text = self._format_grounding(app_code, app_obj, page_names)
        session.context["_preflight_grounding"] = text
        return text

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
