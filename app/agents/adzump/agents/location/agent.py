"""LocationAgent — sub-agent for campaign geo-targeting.

Lives behind the ``manage_targeting_locations`` tool exposed to the AdPilot
chat agent. Three public actions:

- ``discover()`` — LLM-driven. Runs a BaseAgent loop with two tools
  (``discover_neighborhoods`` for local, ``geocode_recommendations`` for broad);
  the LLM picks the path/markets and writes a 1-2 sentence summary.
- ``add()`` / ``delete()`` — deterministic. No LLM, no loop. The widget's map
  clicks and the orchestrator's edit calls land here directly.

All three end in ``tools._shared.finalize_targets`` (map → persist → re-render)
— one source of truth for "targets changed".

Design notes (mirrors ProductAgent):
- Isolated sub-session for discover (own message history, own token audit).
- Wrapped event stream — tool/craft/data events pass through to the parent,
  text/done/error are silenced (the parent owns those).
- Launcher owns the AgentCard lifecycle (agent_started/finished).
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.agent import BaseAgent
from app.core.session import BaseSession
from app.core.streaming import AgentEventStream, pre_emit_agent_started
from app.core.tools.base import ToolResult
from app.agents.adzump._shared import product_location_str
from app.agents.adzump.adapters.google.maps import google_maps_client
from app.agents.adzump.agents.location.context import build_location_context
from app.agents.adzump.agents.location.tools import LOCATION_AGENT_TOOLS
from app.agents.adzump.agents.location.tools._shared import finalize_targets

logger = logging.getLogger(__name__)

LOCATION_PROVIDER = "anthropic"
LOCATION_MODEL_TIER = "fast"
LOCATION_MAX_TURNS = 10  # 1 reasoning + 1 tool + 1 summary + slack
LOCATION_MAX_TOKENS = 4096


class _LocationPassthroughEventStream(AgentEventStream):
    """Event stream wrapper used by the location sub-agent.

    Forwards user-visible progress (tool_*, craft, data, agent_*, thinking) to
    the parent stream and drops everything else (text, done, error) — the
    parent agent owns those.
    """

    def __init__(self, parent: AgentEventStream) -> None:
        # Deliberately do NOT call super().__init__(): we don't want a local
        # queue; this wrapper only delegates to the parent stream.
        self._parent = parent

    @property
    def is_cancelled(self) -> bool:
        # Delegate to parent so a top-level user cancel propagates into the
        # sub-agent's run loop.
        return getattr(self._parent, "is_cancelled", False)

    def cancel(self) -> None:
        try:
            self._parent.cancel()
        except Exception:
            pass

    async def emit_text(self, text: str) -> None:
        # The sub-agent's text is its summary — the parent surfaces it via the
        # ToolResult, not as chat text.
        return

    async def emit_thinking(self, reasoning: str) -> None:
        await self._parent.emit_thinking(reasoning)

    async def emit_tool_start(self, tool_name, tool_input, tool_use_id="", display_name="") -> None:
        await self._parent.emit_tool_start(tool_name, tool_input, tool_use_id, display_name)

    async def emit_tool_update(self, tool_use_id: str, message: str) -> None:
        await self._parent.emit_tool_update(tool_use_id, message)

    async def emit_tool_result(self, tool_name, success, summary, tool_use_id="") -> None:
        await self._parent.emit_tool_result(tool_name, success, summary, tool_use_id)

    async def emit_error(self, message: str) -> None:
        # Swallow: the parent tool wrapper converts failures into a ToolResult.
        logger.debug("location_substream_error: %s", message[:200])

    async def emit_done(self, session_id: str = "", usage: dict | None = None) -> None:
        return

    async def emit_keepalive(self) -> None:
        return

    async def emit_suggestions(self, options, mode="single") -> None:
        return

    async def emit_data(self, data_type: str, payload: dict) -> None:
        await self._parent.emit_data(data_type, payload)

    async def emit_agent_started(self, agent_id: str, label: str, parent_id: str = "root",
                                 parent_tool_use_id: str = "",
                                 agent_tool_use_id: str = "") -> None:
        await self._parent.emit_agent_started(
            agent_id, label, parent_id, parent_tool_use_id,
            agent_tool_use_id=agent_tool_use_id,
        )

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


def _build_initial_prompt(
    product: dict, location_name: str, country_code: str
) -> str:
    """User prompt for one discover run — the business profile the LLM plans from."""
    summary = (product.get("summary") or "").strip()
    if len(summary) > 600:
        summary = summary[:600] + "…"
    return (
        "Set the geographic targeting for this campaign now.\n\n"
        f"- Business: {product.get('product_name') or '(unknown)'}\n"
        f"- Category: {product.get('business_type') or '(unknown)'}\n"
        f"- Operating scale: {(product.get('business_scale') or 'national').strip().lower()}\n"
        f"- Target country: {country_code}\n"
        f"- Confirmed location: {location_name or '(none)'}\n"
        f"- Summary: {summary or '(none)'}\n\n"
        "Pick the right tool for the operating scale, call it once, then write "
        "your 1-2 sentence summary."
    )


class LocationAgent(BaseAgent):
    """Sub-agent that owns campaign geo-targeting (discover / add / delete)."""

    display_name = "Location Agent"

    _instance: "LocationAgent | None" = None

    def __init__(self) -> None:
        context = build_location_context()

        super().__init__(
            name="location_agent",
            tools=LOCATION_AGENT_TOOLS,
            context_builder=context,
            model_tier=LOCATION_MODEL_TIER,
            max_turns=LOCATION_MAX_TURNS,
            max_tokens=LOCATION_MAX_TOKENS,
            provider=LOCATION_PROVIDER,
        )

    @classmethod
    def get_instance(cls) -> "LocationAgent":
        if cls._instance is None:
            cls._instance = cls()
            logger.info("LocationAgent created with %d tools", len(LOCATION_AGENT_TOOLS))
        return cls._instance

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        """Expose the (sub-)session state to the location tools."""
        ctx = super().build_tool_context(session)
        ctx["session_context"] = session.context
        ctx["_session"] = session
        if session.auth:
            ctx["auth"] = session.auth
        return ctx

    # ── discover — the LLM-driven flow ────────────────────────────────────

    async def discover(self, params: dict, context: dict) -> ToolResult:
        """Geocode the session location, then run the loop: pick path → tool → summary."""
        parent_ctx = context.get("session_context") or {}
        product = parent_ctx.setdefault("product_data", {})
        spec = parent_ctx.setdefault("campaign_spec", {})
        loc_meta = parent_ctx.setdefault("_location_meta", {})

        stream = context.get("event_stream")
        tool_use_id = context.get("tool_use_id", "")
        auth = context.get("auth")
        if auth is None:
            return ToolResult(
                success=False,
                error="No auth context available for the location agent.",
            )

        # ── Deterministic preamble: resolve + geocode the campaign location so
        # the radial-scan tool has coordinates. No LLM involved. ──
        location_name = params.get("location_name")
        if not location_name:
            location_name = (
                loc_meta.get("address")
                or spec.get("location")
                or product_location_str(product)
            )

        coordinates = None
        if loc_meta.get("lat") is not None and loc_meta.get("lng") is not None:
            coordinates = {"lat": float(loc_meta["lat"]), "lng": float(loc_meta["lng"])}

        if not coordinates and location_name:
            try:
                geo = await google_maps_client.geocode(location_name)
                if geo and geo.get("lat") is not None and geo.get("lng") is not None:
                    coordinates = {"lat": float(geo["lat"]), "lng": float(geo["lng"])}
                    loc_meta["lat"] = geo["lat"]
                    loc_meta["lng"] = geo["lng"]
                    if geo.get("country_code"):
                        loc_meta["country_code"] = geo["country_code"]
                    if not loc_meta.get("address") and geo.get("address"):
                        loc_meta["address"] = geo["address"]
                    if "place_id" in geo:
                        loc_meta["place_id"] = geo["place_id"]
            except Exception as ge:
                logger.warning("Geocoding '%s' failed: %s", location_name, ge)

        if coordinates:
            product["product_coordinates"] = coordinates

        # ── Sub-session with selective context sharing. Shared dict refs let the
        # tools write through to the parent (same objects in memory); the keys
        # cover everything finalize_targets/save_campaign read. The sub-agent's
        # MESSAGE HISTORY stays isolated — that's the real isolation win. ──
        sub_session = BaseSession(agent_name="location_agent")
        await sub_session.get_or_create(None, auth)
        shared: dict[str, Any] = {
            "product_data": product,
            "product_profile": parent_ctx.setdefault("product_profile", {}),
            "campaign_spec": spec,
            "_location_meta": loc_meta,
            "account_names": parent_ctx.setdefault("account_names", {}),
            "craft_id": parent_ctx.get("craft_id", ""),
            "_craft_id": parent_ctx.get("_craft_id", ""),
            # Parent chat session id — save_campaign stamps it on the record.
            "_session_id": parent_ctx.get("_session_id", ""),
        }
        if isinstance(parent_ctx.get("competitor_analysis"), dict):
            shared["competitor_analysis"] = parent_ctx["competitor_analysis"]
        sub_session.context = shared

        # Launcher owns both AgentCard ends: agent_started here, finished below.
        if stream is not None:
            await pre_emit_agent_started(
                stream, agent_id="location_agent", label="Location Agent",
                parent_tool_use_id=tool_use_id, context=parent_ctx,
            )
        wrapped = _LocationPassthroughEventStream(stream) if stream else AgentEventStream()

        country_code = loc_meta.get("country_code") or "IN"
        status = "success"
        try:
            await self.run(
                user_message=_build_initial_prompt(product, location_name or "", country_code),
                session=sub_session,
                event_stream=wrapped,
            )
        except Exception as e:
            status = "failed"
            logger.exception("LocationAgent discover run failed: %s", e)
        finally:
            if stream is not None:
                try:
                    await stream.emit_agent_finished("location_agent", status=status)
                except Exception:
                    pass

        # The final assistant text is the 1-2 sentence summary for the parent.
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

        # finalize_targets stamps this on the SUB context when a tool landed
        # targets — the honest success signal (a chatty run that never called a
        # tool must not read as success).
        if not sub_session.context.get("_geo_finalized"):
            return ToolResult(
                success=False,
                error=(
                    "The location agent finished without resolving any targeting "
                    "areas. Retry manage_targeting_locations(action=\"discover\") "
                    "or ask the user for their target location."
                ),
            )

        mapped = product.get("target_areas") or []
        platform = (spec.get("platform") or "Google Ads").strip()
        logger.info(
            "location_agent.discover: %d areas for platform=%s", len(mapped), platform,
        )
        return ToolResult(
            success=True,
            data={"target_areas": mapped, "summary": final_text},
            summary=(
                final_text.strip()
                or f"Discovered and mapped {len(mapped)} targeting locations for {platform}."
            ),
        )

    # ── add / delete — the deterministic flow (no LLM, no loop) ──────────

    async def add(self, params: dict, context: dict) -> ToolResult:
        """Append a targeting area and re-sync platform handles + craft panel."""
        session_ctx = context.get("session_context")
        if session_ctx is None:
            return ToolResult(success=False, error="No session context available.")

        name = params.get("name")
        if not name:
            return ToolResult(success=False, error="Name is required for 'add' action.")

        product = session_ctx.setdefault("product_data", {})
        target_areas = product.setdefault("target_areas", [])

        area: dict = {
            "name": name,
            "city": params.get("city") or "",
            "state": params.get("state") or "",
            "pincode": params.get("pincode") or "",
            "lat": params.get("lat"),
            "lng": params.get("lng"),
            "distance_km": params.get("radius") or 5.0,
        }
        if params.get("place_id"):
            area["place_id"] = params["place_id"]
        if params.get("google_id"):
            area["google_id"] = params["google_id"]
            area["google_name"] = name
        if params.get("meta_key"):
            area["meta_key"] = params["meta_key"]
            area["meta_name"] = name
        # meta_type has no LLM tool param — it only arrives via the search-widget
        # JSON. The mapper re-derives it when absent, so this just preserves it.
        if params.get("meta_type"):
            area["meta_type"] = params["meta_type"]
        target_areas.append(area)

        mapped = await finalize_targets(target_areas, context)
        logger.info("location_agent.add: name=%r areas=%d", name, len(mapped))
        return ToolResult(
            success=True,
            data={"target_areas": mapped},
            summary="Successfully added targeting area.",
        )

    async def delete(self, params: dict, context: dict) -> ToolResult:
        """Remove a targeting area by 1-based index and re-sync."""
        session_ctx = context.get("session_context")
        if session_ctx is None:
            return ToolResult(success=False, error="No session context available.")

        product = session_ctx.setdefault("product_data", {})
        target_areas = product.setdefault("target_areas", [])

        index = params.get("index")
        if index is None or index < 1 or index > len(target_areas):
            return ToolResult(
                success=False,
                error=(
                    f"Invalid index {index}. There are only {len(target_areas)} "
                    "target areas."
                ),
            )
        target_areas.pop(index - 1)

        mapped = await finalize_targets(target_areas, context)
        logger.info("location_agent.delete: index=%s areas=%d", index, len(mapped))
        return ToolResult(
            success=True,
            data={"target_areas": mapped},
            summary="Successfully deleted targeting area.",
        )


def get_location_agent() -> LocationAgent:
    """Module-level accessor for the shared LocationAgent singleton."""
    return LocationAgent.get_instance()
