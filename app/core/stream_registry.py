"""Reach a running SSE stream from a later HTTP request.

`AgentEventStream` already knows how to be cancelled and how to resolve a pending
tool confirmation, but both are in-process calls and the caller arrives on a
separate request. This module is the address book that connects the two.

The complication is that production runs gunicorn with four uvicorn workers
(see Dockerfile), and the POST that answers a confirmation lands on whichever
worker the load balancer picks, not the one holding the stream. A plain
dictionary would therefore work about one time in four. So a signal that finds
no local stream is republished on Redis, and every worker listens and applies
the ones it owns.

Redis is optional: `get_redis_client()` returns None when it is disabled or
unreachable, and everything below degrades to the local dictionary, which is
correct for a single-process run.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.core.streaming import AgentEventStream

logger = logging.getLogger(__name__)

_CHANNEL = "ai:agent:control"

# session_id -> the stream currently serving it, for streams on THIS worker.
_local_streams: dict[str, AgentEventStream] = {}

_subscriber_task: asyncio.Task | None = None


def register(session_id: str, stream: AgentEventStream) -> None:
    if session_id:
        _local_streams[session_id] = stream


def unregister(session_id: str) -> None:
    _local_streams.pop(session_id, None)


def _apply(session_id: str, action: str, payload: dict[str, Any]) -> bool:
    """Apply a control signal to a locally held stream. False if not ours."""
    stream = _local_streams.get(session_id)
    if stream is None:
        return False

    if action == "stop":
        # Prefer the run: it sets the same cancelled flag and then hard-cancels
        # if the flag goes unnoticed, which is what a stop pressed during a
        # slow tool call needs. Nothing else may end a run early: a client
        # that merely disconnects must leave it alone.
        from app.core import run_manager

        run = run_manager.get_local_run(session_id)
        if run is not None:
            run.request_stop()
        else:
            stream.cancel()
        return True

    if action == "confirm":
        confirmation_id = payload.get("confirmation_id")
        if not confirmation_id:
            return False
        return stream.resolve_confirmation(
            confirmation_id,
            {
                "approved": bool(payload.get("approved")),
                "selected": payload.get("selected")
                or ("approve" if payload.get("approved") else "deny"),
            },
        )

    return False


async def signal(session_id: str, action: str, payload: dict[str, Any] | None = None) -> str:
    """Deliver a control signal to the stream serving `session_id`.

    Returns what actually happened, because the three cases are genuinely
    different and callers must not conflate them:

      "local"     — a stream on this worker took it. Definitely delivered.
      "broadcast" — no local stream, so it went out on Redis for a sibling
                    worker. Whether anything picked it up is unknowable from
                    here; do not report this as success.
      "missing"   — no local stream and no Redis, so there is nothing else it
                    could have been. Definitely not delivered.
    """
    payload = payload or {}
    if _apply(session_id, action, payload):
        return "local"

    from app.services.redis_client import get_redis_client

    redis = await get_redis_client()
    if redis is None:
        # Single process, or Redis is down: the local miss is the real answer.
        return "missing"

    try:
        await redis.publish(
            _CHANNEL,
            json.dumps({"session_id": session_id, "action": action, "payload": payload}),
        )
        return "broadcast"
    except Exception:  # noqa: BLE001 — a control signal must never 500 the caller
        logger.warning("stream_registry: publish failed for %s/%s", session_id, action, exc_info=True)
        return "missing"


async def _listen() -> None:
    from app.services.redis_client import get_redis_client

    redis = await get_redis_client()
    if redis is None:
        return

    pubsub = redis.pubsub()
    await pubsub.subscribe(_CHANNEL)
    logger.info("stream_registry: listening on %s", _CHANNEL)

    async for message in pubsub.listen():
        if message.get("type") != "message":
            continue
        try:
            data = json.loads(message["data"])
            _apply(data["session_id"], data["action"], data.get("payload") or {})
        except Exception:  # noqa: BLE001 — one bad message must not kill the listener
            logger.warning("stream_registry: bad control message", exc_info=True)


async def start_subscriber() -> None:
    """Begin listening for signals aimed at streams on this worker."""
    global _subscriber_task
    if _subscriber_task is not None:
        return
    _subscriber_task = asyncio.create_task(_listen())


async def stop_subscriber() -> None:
    global _subscriber_task
    if _subscriber_task is None:
        return
    _subscriber_task.cancel()
    _subscriber_task = None
