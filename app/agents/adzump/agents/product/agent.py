"""ProductAgent — sub-agent for deep business + competitor analysis.

Lives behind the ``analyze_business`` tool exposed to the AdPilot chat agent.
Runs a tool loop (scrape_url + Anthropic built-in web_search + shortlist)
against Claude Sonnet 4.6, then returns a structured JSON analysis.

Design notes:
- Isolated sub-session (own message history, own DB row).
- Wrapped event stream — craft + progress passes through to the parent,
  done/error are silenced (the parent owns those).
- Hard-capped at 25 turns, balanced model tier.
- If anything goes wrong the caller (tools/business.py) falls back to the
  deterministic ScrapePipeline, so a broken agent never blocks the
  user's campaign flow.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.agent import BaseAgent
from app.core.session import BaseSession, AuthContext
from app.core.streaming import AgentEventStream
from app.agents.adzump.agents.product.context import build_product_context
from app.agents.adzump.agents.product.tools import PRODUCT_TOOLS
from app.agents.adzump.agents.product.models import AnalysisOutput
from app.agents.adzump.tools._shared import extract_json

logger = logging.getLogger(__name__)

# Anthropic Sonnet 4.6 handles the 25-turn research flow with the built-in
# server-side web_search tool (see tools/__init__.py). Pinned to Anthropic
# so the server tool is available — other providers lack this capability.
ANALYST_PROVIDER = "anthropic"
ANALYST_MODEL_TIER = "balanced"  # kept for the BaseAgent constructor
ANALYST_MODEL_OVERRIDE = "anthropic:claude-sonnet-4-6"
ANALYST_MAX_TURNS = 25  # 1 scrape + multiple web_search + 3-5 web_fetch + final JSON turn + slack
# The final-JSON turn emits 6-12 competitors with segments, positioning,
# USPs, and narratives — easily 3-5K output tokens. 4K truncated mid-JSON
# and left us with unparseable output (stop_reason=max_tokens).
ANALYST_MAX_TOKENS = 16384


def _build_minimal_result(primary_url: str, session_ctx: dict) -> dict | None:
    """Construct a minimal result from whatever the sub-session accumulated.

    Used when the agent fails to produce valid JSON but has partial state
    (screenshots, search snippets) worth salvaging.
    """
    if not session_ctx:
        return None

    product_data = session_ctx.get("product_data") or {}
    research_state = session_ctx.get("_research_state") or {}
    merged_searches = (
        research_state.get("search_results_merged")
        or product_data.get("search_results_merged")
        or []
    )
    primary_screenshot = product_data.get("primary_screenshot_url")

    search_snippets: list[str] = []
    for r in merged_searches:
        if isinstance(r, dict) and r.get("text"):
            search_snippets.append(f"[{r.get('query','')}]\n{r['text']}")

    from urllib.parse import urlparse as _urlparse
    host = ""
    try:
        host = _urlparse(primary_url).netloc.removeprefix("www.")
    except Exception:
        pass

    result: dict = {
        "business": {
            "product_name": host or "(unknown)",
            "business_type": "",
            "location": "",
            "suggested_locations": [],
            "summary": (
                "Automated research failed. Please retry or provide details "
                "manually. Search evidence is attached in notes."
            ),
            "unique_features": [],
            "products_services": [],
            "pricing": "",
            "contact": {"phone": "", "email": "", "address": ""},
            "pages_analyzed": [primary_url] if primary_url else [],
        },
        "competitive": {
            "competitors": [],
            "segments": {"direct": [], "adjacent": [], "alternative": []},
            "positioning_narrative": {
                "direct_battlefield": "",
                "wins_against": [],
                "loses_to": [],
            },
            "our_usps": [],
            "competitive_threats": [],
            "keyword_opportunities": [],
            "positioning": "",
        },
        "notes": [],
    }
    if search_snippets:
        combined = "\n\n".join(search_snippets)[:3000]
        result["notes"].append(
            "Raw search evidence (no structured competitors extracted):\n"
            + combined
        )
    if primary_screenshot:
        result["notes"].append(f"primary_screenshot_url={primary_screenshot}")
    return result


class _PassthroughEventStream(AgentEventStream):
    """Event stream wrapper used by the sub-agent.

    Forwards user-visible progress (craft + tool_update rewritten to the
    parent's tool_use_id) to the parent stream, and drops everything else
    (text, tool_start/result, done, error) — the parent agent owns those.
    """

    def __init__(self, parent: AgentEventStream, parent_tool_use_id: str) -> None:
        # Deliberately do NOT call super().__init__(): we don't want a local
        # queue; this wrapper only delegates to the parent stream.
        self._parent = parent
        self._parent_tool_use_id = parent_tool_use_id

    @property
    def is_cancelled(self) -> bool:
        # Delegate to parent so a top-level user cancel propagates into the
        # sub-agent's run loop. Without this, BaseAgent._run_loop's
        # `event_stream.is_cancelled` check raises AttributeError because
        # we skip super().__init__() (which would set self._cancelled = False).
        return getattr(self._parent, "is_cancelled", False)

    def cancel(self) -> None:
        try:
            self._parent.cancel()
        except Exception:
            pass

    async def emit_text(self, text: str) -> None:
        # The sub-agent's text is the JSON output — don't leak it to the user.
        return

    async def emit_thinking(self, reasoning: str) -> None:
        # Forward analyst's reasoning so it appears inside the agent card.
        await self._parent.emit_thinking(reasoning)

    async def emit_tool_start(self, tool_name, tool_input, tool_use_id="", display_name="") -> None:
        # Forward analyst's tool calls so they nest inside the agent card.
        await self._parent.emit_tool_start(tool_name, tool_input, tool_use_id, display_name)

    async def emit_tool_update(self, tool_use_id: str, message: str) -> None:
        # Forward to the analyst's own tool_use_id (now visible to the user).
        await self._parent.emit_tool_update(tool_use_id, message)

    async def emit_tool_result(self, tool_name, success, summary, tool_use_id="") -> None:
        # Forward analyst's tool results so they render inside the agent card.
        await self._parent.emit_tool_result(tool_name, success, summary, tool_use_id)

    async def emit_error(self, message: str) -> None:
        # Swallow: parent tool wrapper converts failures into a ToolResult.
        logger.debug("analyst_substream_error: %s", message[:200])

    async def emit_done(self, session_id: str = "", usage: dict | None = None) -> None:
        return

    async def emit_keepalive(self) -> None:
        return

    async def emit_suggestions(self, options, mode="single") -> None:
        return

    async def emit_data(self, data_type: str, payload: dict) -> None:
        await self._parent.emit_data(data_type, payload)

    async def emit_agent_started(self, agent_id: str, label: str, parent_id: str = "root",
                                 parent_tool_use_id: str = "") -> None:
        await self._parent.emit_agent_started(agent_id, label, parent_id, parent_tool_use_id)

    async def emit_agent_finished(self, agent_id: str, status: str = "success",
                                  duration_ms: int = 0, tokens_in: int = 0, tokens_out: int = 0,
                                  step_count: int = 0, summary: str = "") -> None:
        await self._parent.emit_agent_finished(
            agent_id, status, duration_ms, tokens_in, tokens_out, step_count, summary,
        )

    async def emit_agent_usage(self, agent_id: str, tokens_in: int, tokens_out: int) -> None:
        await self._parent.emit_agent_usage(agent_id, tokens_in, tokens_out)

    async def emit_craft(self, craft_id, title, blocks, message_id="", append=False) -> None:
        await self._parent.emit_craft(craft_id, title, blocks, message_id=message_id, append=append)

    async def emit_craft_text(self, craft_id: str, text_delta: str) -> None:
        await self._parent.emit_craft_text(craft_id, text_delta)

    async def emit_feedback_request(self, session_id: str, turn_number: int) -> None:
        return


class ProductAgent(BaseAgent):
    """Sub-agent that does business understanding + competitor discovery."""

    display_name = "Product Analyst"

    _instance: "ProductAgent | None" = None

    def __init__(self) -> None:
        context = build_product_context()
        # Skip the async doc load step — static_prefix is all we have.
        context._cached_static_text = context._static_prefix

        super().__init__(
            name="product_analyst",
            tools=PRODUCT_TOOLS,
            context_builder=context,
            model_tier=ANALYST_MODEL_TIER,
            max_turns=ANALYST_MAX_TURNS,
            max_tokens=ANALYST_MAX_TOKENS,
            provider=ANALYST_PROVIDER,
            sequential_tools=False,
            context_management={
                "edits": [{
                    "type": "clear_tool_uses_20250919",
                    "trigger": {"type": "input_tokens", "value": 15000},
                    "keep": {"type": "tool_uses", "value": 2},
                    "clear_at_least": {"type": "input_tokens", "value": 30000},
                }],
            },
        )

    @classmethod
    def get_instance(cls) -> "ProductAgent":
        if cls._instance is None:
            cls._instance = cls()
            logger.info("ProductAgent created with %d tools", len(PRODUCT_TOOLS))
        return cls._instance

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        """Expose session state to analyst tools (for craft sharing + history access)."""
        ctx = super().build_tool_context(session)
        ctx["session_context"] = session.context
        # shortlist_competitors pulls candidates out of Anthropic
        # web_search_tool_result blocks, which live only in message history.
        ctx["session_messages"] = session.get_messages
        return ctx

    def _parse_result(
        self, final_text: str, sub_session: BaseSession, primary_url: str,
    ) -> AnalysisOutput:
        """Extract structured output from the agent's final text + session state."""
        payload = extract_json(final_text)
        if not payload or "business" not in payload:
            payload = _build_minimal_result(primary_url, sub_session.context)

        business = payload.get("business") if payload else None
        competitive = payload.get("competitive") if payload else None
        notes = payload.get("notes", []) if payload else []
        screenshot_url = (
            (sub_session.context.get("product_data") or {})
            .get("primary_screenshot_url")
        )
        return AnalysisOutput(
            business=business,
            competitive=competitive,
            notes=notes,
            screenshot_url=screenshot_url,
            raw_text=final_text,
        )

    async def analyze(
        self,
        url: str,
        parent_event_stream: AgentEventStream,
        parent_tool_use_id: str,
        auth: AuthContext,
        parent_session_context: dict | None = None,
        user_message: str | None = None,
    ) -> AnalysisOutput:
        """Run one analysis and return structured output.

        The caller controls scope via ``user_message``:
        - Profile only: "Scrape and profile this business. Do NOT search for competitors."
        - Full research: "Run competitor research for this business using web_search and web_fetch."
        """
        sub_session = BaseSession(agent_name="product_analyst")
        await sub_session.get_or_create(None, auth)

        # Selective context sharing: the sub-agent gets references to only
        # the dicts it needs. Writes inside these propagate back to the
        # parent (same dict objects in memory). The sub-agent CANNOT
        # accidentally touch campaign_data or other chat-agent-owned state.
        if parent_session_context is not None:
            product_data = parent_session_context.setdefault("product_data", {})
            product_data["primary_url"] = url
            # Reset per-run counters; the parent may reuse its session context
            # across multiple analyses.
            product_data["scrape_count"] = 0
            product_data["scraped_urls"] = []
            product_data.pop("primary_screenshot_url", None)

            sub_session.context = {
                "product_data": product_data,
                "product_profile": parent_session_context.setdefault("product_profile", {}),
                "research_state": parent_session_context.setdefault("research_state", {}),
                "craft_id": parent_session_context.get("craft_id", ""),
            }

        wrapped_stream = _PassthroughEventStream(parent_event_stream, parent_tool_use_id)

        msg = user_message or f"Analyze this business: {url}"
        await self.run(
            user_message=msg,
            session=sub_session,
            event_stream=wrapped_stream,
            model_override=ANALYST_MODEL_OVERRIDE,
            parent_tool_use_id=parent_tool_use_id,
        )

        # Find the last assistant message's text — that's the JSON output.
        final_text = ""
        for msg in reversed(sub_session.get_messages()):
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                final_text = content
                break
            if isinstance(content, list):
                parts = [c.get("text", "") for c in content if c.get("type") == "text"]
                if any(parts):
                    final_text = "\n".join(p for p in parts if p)
                    break

        return self._parse_result(final_text, sub_session, url)


def get_product_agent() -> ProductAgent:
    """Module-level accessor for the shared analyst singleton."""
    return ProductAgent.get_instance()

