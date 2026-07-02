"""Location widget protocol — parsing AND handling.

The craft-panel map search widget sends machine-readable messages:

  "add targeting location {"name":"<name>","lat":<lat>,"lng":<lng>,...}"  (JSON payload)
  "delete targeting location index <n>"                                   (1-based)

These are NOT natural language — they carry all the parameters needed to
call manage_targeting_locations directly, with no LLM required. The whole
protocol lives here (geo layer): ``handle_widget_message`` is the single
entry point the HTTP router forwards to — it owns parsing, elicitation
housekeeping, dispatch to the GeoTargetingService, and SSE emission, so the
router stays a dumb forwarder and the loop's invariants live in one place.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import re
from typing import Any

from fastapi.responses import StreamingResponse

from app.core.streaming import AgentEventStream
from app.agents.adzump.agents.geo.agent import get_geo_targeting_service

logger = logging.getLogger(__name__)

_ADD_PREFIXES = ("add targeting location ", "adding location ")
_DELETE_PREFIXES = (
    "remove targeting location ",
    "delete targeting location ",
    "removing location ",
    "deleting location ",
)

_JSON_FIELD_MAP = {
    "name": str,
    "lat": float,
    "lng": float,
    "place_id": str,
    "pincode": str,
    "city": str,
    "state": str,
    "google_id": str,
    "meta_key": str,
    "meta_type": str,
}


def parse_location_widget_message(msg: str) -> dict[str, Any] | None:
    """Return parsed params if msg is a location widget message, else None.

    On match returns a dict with 'action' set to "add" or "delete" plus any
    of: name, lat, lng, place_id, city, state, pincode, google_id, meta_key,
    meta_type, index.  Returns None for natural-language messages that should
    go through the normal agent loop.
    """
    lower = msg.strip().lower()
    if any(lower.startswith(p) for p in _ADD_PREFIXES):
        return {"action": "add", **_parse_params(msg)}
    if any(lower.startswith(p) for p in _DELETE_PREFIXES):
        return {"action": "delete", **_parse_params(msg)}
    return None


def _parse_params(msg: str) -> dict[str, Any]:
    # Primary format: JSON payload  e.g. add targeting location {"name":"...","lat":...}
    json_start = msg.find('{')
    if json_start != -1:
        try:
            payload = _json.loads(msg[json_start:])
            params: dict[str, Any] = {}
            for field, cast in _JSON_FIELD_MAP.items():
                val = payload.get(field)
                if val is not None:
                    try:
                        params[field] = cast(val)
                    except (TypeError, ValueError):
                        pass
            if "index" in payload:
                try:
                    params["index"] = int(payload["index"])
                except (TypeError, ValueError):
                    pass
            return params
        except _json.JSONDecodeError:
            pass

    # Fallback: key=value or "index <n>" formats
    params = {}

    for key in ("name", "place_id", "city", "state", "pincode", "google_id", "meta_key", "meta_type"):
        m = re.search(rf'{key}="([^"]*)"', msg)
        if m:
            params[key] = m.group(1)
        else:
            m = re.search(rf'{key}=(\S+)', msg)
            if m:
                params[key] = m.group(1)

    for key in ("lat", "lng"):
        m = re.search(rf'{key}=([+-]?\d+\.?\d*)', msg)
        if m:
            try:
                params[key] = float(m.group(1))
            except ValueError:
                pass

    # Accept both "index=5" and "index 5" (frontend sends the latter for delete)
    m = re.search(r'index[= ](\d+)', msg)
    if m:
        try:
            params["index"] = int(m.group(1))
        except ValueError:
            pass

    return params


# ── Handling: the use-case the HTTP router forwards to ─────────────────────


def handle_widget_message(agent, session, message: str) -> StreamingResponse | None:
    """Widget-protocol entry point for the chat router.

    Returns None when ``message`` is not a widget message (normal chat → the
    agent loop). Otherwise executes the action directly — no LLM — and returns
    the SSE response.
    """
    params = parse_location_widget_message(message)
    if params is None:
        return None
    return _stream_widget_action(agent, session, params)


def _stream_widget_action(agent, session, params: dict) -> StreamingResponse:
    """Execute a location widget action directly (no LLM) and stream SSE."""
    event_stream = AgentEventStream()

    async def run() -> None:
        try:
            # Same tool context the agent would pass to any tool.
            ctx = agent.build_tool_context(session)
            ctx["event_stream"] = event_stream
            # Clear any pending chip-question so the next real agent turn
            # doesn't re-ask duration/budget after a location add/delete.
            session.context.pop("_pending_elicitation", None)

            await event_stream.emit_tool_start(
                tool_use_id="widget_location",
                tool_name="manage_targeting_locations",
                display_name="Geo Targeting",
                tool_input=params,
            )

            result = await get_geo_targeting_service().modify(params, ctx)
            # modify() owns save_campaign + _rerender_craft; nothing extra needed here.

            await event_stream.emit_tool_result(
                tool_use_id="widget_location",
                tool_name="manage_targeting_locations",
                success=result.success,
                summary=result.summary or result.error or "",
            )
        except Exception as e:
            logger.exception("Location widget action failed")
            await event_stream.emit_error(str(e))
        finally:
            await event_stream.emit_done(session_id=session.session_id)

    async def event_generator():
        task = asyncio.create_task(run())
        try:
            async for event in event_stream.events():
                yield event.to_sse()
        except asyncio.CancelledError:
            task.cancel()
            raise
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(event_generator(), media_type="text/event-stream")
