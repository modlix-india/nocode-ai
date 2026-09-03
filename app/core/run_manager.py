"""Agent runs that outlive the request that started them.

A chat used to be one HTTP request: the POST /chat generator consumed the
agent's event stream, and when the browser went away Starlette cancelled that
generator, which cancelled the agent task. Closing a panel, switching sidekick
sessions or simply refreshing therefore killed the run mid-tool, and
``BaseAgent.run``'s CancelledError path wrote the turn to history as
"[Stopped by user]". Work in flight was lost and the transcript lied about why.

Here a run is its own object with its own lifetime:

    POST /chat   → start_run()  → run keeps going regardless of the request
    (disconnect) → subscriber goes away, run does not notice
    POST /attach → subscribe()  → replays the turn so far, then tails it live
    POST /stop   → request_stop() → the only thing that ends a run early

Every event the agent emits is pumped once, into a replay buffer and out to
each attached subscriber. Text deltas are coalesced in the buffer (thousands of
token events collapse into a handful of entries), so replaying a whole turn on
reattach is cheap and, crucially, complete.

**Attach always replays the entire current turn** and the client rebuilds the
assistant message from scratch. There is deliberately no resume-from-cursor:
coalescing rewrites the boundaries between text events, so a cursor into them
cannot mean anything stable. Replayed events are bracketed by `replay_start` /
`replay_end` so a client can suppress the side-effecting ones (a `complete`
that redirects the page must not fire twice).

Production runs four uvicorn workers behind one gunicorn master with no
affinity, so an attach lands on the worker holding the run about one time in
four. Every event is therefore mirrored to a Redis stream and a worker with no
local run serves the attach from there. Redis is optional: with it off
(`REDIS_ENABLED` false, which is the local default) everything falls back to
the in-process buffer, which is correct for a single process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from typing import Any, AsyncIterator

from app.core.streaming import AgentEvent, AgentEventType, AgentEventStream

logger = logging.getLogger(__name__)

# How long a finished run stays replayable. A refresh moments after the last
# token should still show the answer rather than an empty chat.
FINISHED_RETENTION_S = 600

# Replay buffer cap. With text coalescing this is thousands of tool calls, not
# thousands of tokens, so a normal turn never comes near it.
BUFFER_MAX = 4000

# A subscriber that falls this far behind is dropped rather than allowed to
# grow without bound. It reattaches and replays; nothing is lost.
SUBSCRIBER_QUEUE_MAX = 2000

# Gap after which an attached client is sent a keepalive so proxies and the
# client's own watchdog can tell a quiet run from a dead one.
KEEPALIVE_S = 15.0

# Liveness. Deliberately short: this key is what tells another worker a run is
# still going, and a worker that dies mid-turn cannot retract it. Until it
# expires, that session refuses new messages with a 409, so an hour-long TTL
# would lock the chat out for an hour. The heartbeat below refreshes it well
# inside the window.
REDIS_LIVENESS_TTL_S = 120
HEARTBEAT_S = 40

# The mirrored event log. Outlives the run so a client that reattaches just
# after the last token still gets the whole turn replayed; a run that lasts
# longer than this keeps having it refreshed.
REDIS_STREAM_TTL_S = FINISHED_RETENTION_S

# Grace between a stop request and hard-cancelling the task. The agent checks
# its cancelled flag at every loop checkpoint and mid-token, so a stop lands
# promptly on its own; this only covers a stop arriving during a long tool call.
STOP_GRACE_S = 8.0

_STREAM_KEY = "ai:run:{sid}"
_META_KEY = "ai:run:{sid}:meta"
_USER_RUNS_KEY = "ai:runs:user:{uid}"

# Ends a subscriber's iteration.
_EOS = object()

# session_id -> run, for runs on THIS worker.
_runs: dict[str, AgentRun] = {}


class RunAlreadyActive(Exception):
    """A run is already in flight for this session.

    Raised instead of starting a second one: two agents interleaving tool calls
    and history writes on one session corrupts both. The caller turns this into
    a 409 so a second tab is told to attach rather than to send.
    """

    def __init__(self, session_id: str, run_id: str) -> None:
        super().__init__(f"Run {run_id} already active for session {session_id}")
        self.session_id = session_id
        self.run_id = run_id


class AgentRun:
    """One agent turn, detached from whatever request asked for it."""

    def __init__(
        self,
        session_id: str,
        user_id: str,
        agent_name: str,
        stream: AgentEventStream,
        app_code: str = "",
    ) -> None:
        self.session_id = session_id
        self.run_id = uuid.uuid4().hex[:12]
        self.user_id = user_id
        self.agent_name = agent_name
        self.app_code = app_code
        self.stream = stream

        self.status = "running"
        self.started_at = time.time()
        self.finished_at = 0.0

        self._buffer: deque[AgentEvent] = deque(maxlen=BUFFER_MAX)
        self._subscribers: set[asyncio.Queue] = set()
        self._agent_task: asyncio.Task | None = None
        self._pump_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._stop_task: asyncio.Task | None = None
        self._mirror_ok = True
        self._stream_ttl_set = False

    # ── Lifecycle ────────────────────────────────────────────────

    def start(
        self,
        agent,
        message: str,
        session,
        image_blocks: list[dict] | None,
        model_override: str | None,
    ) -> None:
        """Launch the agent and the pump that fans its events out.

        Both are bare event-loop tasks, so neither is inside the request's
        cancel scope: that is the whole point of this module. References are
        held on the run so the loop cannot garbage-collect them mid-flight.
        """
        self._agent_task = asyncio.create_task(
            self._run_agent(agent, message, session, image_blocks, model_override),
            name=f"agent-run:{self.session_id}",
        )
        self._pump_task = asyncio.create_task(
            self._pump(), name=f"agent-pump:{self.session_id}"
        )
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat(), name=f"agent-hb:{self.session_id}"
        )

    async def _run_agent(
        self,
        agent,
        message: str,
        session,
        image_blocks: list[dict] | None,
        model_override: str | None,
    ) -> None:
        try:
            await agent.run(
                message, session, self.stream, image_blocks,
                model_override=model_override,
            )
        except asyncio.CancelledError:
            # A hard stop, or worker shutdown. BaseAgent.run has already
            # recorded the turn and emitted done on this path.
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("Agent run failed: session=%s", self.session_id)
            await self.stream.emit_error(str(e))
        finally:
            # Close the stream exactly once, whatever happened, so the pump
            # ends and every attached client sees a terminal event instead of
            # a spinner that never resolves. emit_done is idempotent.
            await self.stream.emit_done(session_id=self.session_id)

    async def _pump(self) -> None:
        """Single consumer of the agent's event stream.

        Everything downstream (the replay buffer, live subscribers, the Redis
        mirror) is fed from here, which is what keeps ordering identical for a
        client that watched live and one that replayed afterwards.
        """
        try:
            async for event in self.stream.events():
                self._fan_out(event)
                self._append(event)
                await self._mirror(event)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Run pump failed: session=%s", self.session_id)
        finally:
            self.status = "finished"
            self.finished_at = time.time()
            for queue in list(self._subscribers):
                _poison(queue)
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
            if self._stop_task and not self._stop_task.done():
                self._stop_task.cancel()
            # Control signals have nothing left to reach. Left registered, a
            # dead run would keep answering /stop with "delivered".
            from app.core import stream_registry

            stream_registry.unregister(self.session_id)
            await self._mark_finished_in_redis()

    def _fan_out(self, event: AgentEvent) -> None:
        for queue in list(self._subscribers):
            if not _offer(queue, event):
                # Hopelessly behind. Drop it; the client reattaches and replays.
                self._subscribers.discard(queue)
                _poison(queue)
                logger.info(
                    "Dropped a slow subscriber on session %s", self.session_id
                )

    def _append(self, event: AgentEvent) -> None:
        """Buffer an event for replay, coalescing consecutive text.

        A turn emits one event per token. Buffering them individually would
        blow the cap on a long answer and make every reattach re-send thousands
        of entries, so a text delta is folded into the previous one when it
        belongs to the same agent. Live subscribers still receive the deltas
        one by one; this touches only what a reattach replays.
        """
        if event.event == AgentEventType.KEEPALIVE:
            # Liveness, not content: each subscriber generates its own during a
            # quiet spell, and buffering them would pad every replay with noise.
            return

        if event.event in (AgentEventType.TEXT, AgentEventType.THINKING):
            last = self._buffer[-1] if self._buffer else None
            if (
                last is not None
                and last.event == event.event
                and last.data.get("agent_id") == event.data.get("agent_id")
            ):
                last.data["text"] = (last.data.get("text") or "") + (
                    event.data.get("text") or ""
                )
                return
            # Buffer a copy, ALWAYS. The same object has already gone out to
            # live subscribers, which serialize it whenever they get to it, so
            # folding the next delta into the original would make a client
            # render text twice.
            event = AgentEvent(event=event.event, data=dict(event.data))
        self._buffer.append(event)

    # ── Stopping ─────────────────────────────────────────────────

    def request_stop(self) -> None:
        """Ask the run to end. The only sanctioned way to cut one short.

        Sets the cancelled flag the agent loop checks, then hard-cancels if the
        run is still going after the grace window: a stop pressed during a
        slow tool call must not appear to be ignored.
        """
        self.stream.cancel()
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._force_stop_after_grace())

    async def _force_stop_after_grace(self) -> None:
        try:
            await asyncio.sleep(STOP_GRACE_S)
        except asyncio.CancelledError:
            return
        task = self._agent_task
        if task and not task.done():
            logger.info(
                "Hard-cancelling session %s: stop not honoured within %.0fs",
                self.session_id, STOP_GRACE_S,
            )
            task.cancel()

    def cancel(self) -> None:
        """Tear the run down without ceremony. Worker shutdown only."""
        for task in (self._agent_task, self._pump_task, self._heartbeat_task, self._stop_task):
            if task and not task.done():
                task.cancel()

    @property
    def is_running(self) -> bool:
        return self.status == "running"

    @property
    def is_expired(self) -> bool:
        return (
            self.status == "finished"
            and time.time() - self.finished_at > FINISHED_RETENTION_S
        )

    def describe(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "agent_name": self.agent_name,
            "app_code": self.app_code,
            "status": self.status,
            "started_at": self.started_at,
            "subscribers": len(self._subscribers),
        }

    # ── Consumer side ────────────────────────────────────────────

    async def subscribe(self) -> AsyncIterator[AgentEvent]:
        """Replay the turn so far, then follow it live.

        The snapshot and the subscription happen with no await between them, so
        the pump cannot slip an event into the gap: nothing is dropped and
        nothing is delivered twice.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        snapshot = list(self._buffer)
        live = self.is_running
        if live:
            self._subscribers.add(queue)

        try:
            yield AgentEvent(
                event=AgentEventType.REPLAY_START,
                data={"run_id": self.run_id, "session_id": self.session_id,
                      "count": len(snapshot)},
            )
            for event in snapshot:
                yield event
            yield AgentEvent(
                event=AgentEventType.REPLAY_END,
                data={"run_id": self.run_id, "session_id": self.session_id,
                      "running": live},
            )

            if not live:
                # Already over: the buffer ends in its own done event, so the
                # client has the whole turn and needs nothing more.
                return

            async for event in _drain(queue):
                yield event
        finally:
            self._subscribers.discard(queue)

    # ── Redis mirror ─────────────────────────────────────────────

    async def _redis(self):
        if not self._mirror_ok:
            return None
        from app.services.redis_client import get_redis_client

        return await get_redis_client()

    async def publish_start(self) -> None:
        """Announce the run so other workers can find and serve it."""
        redis = await self._redis()
        if redis is None:
            return
        try:
            key = _STREAM_KEY.format(sid=self.session_id)
            meta = _META_KEY.format(sid=self.session_id)
            # A previous turn's events must not be replayed as part of this one.
            await redis.delete(key)
            await redis.hset(meta, mapping={
                "run_id": self.run_id,
                "session_id": self.session_id,
                "user_id": self.user_id,
                "agent_name": self.agent_name,
                "app_code": self.app_code,
                "status": "running",
                "started_at": str(self.started_at),
            })
            await redis.expire(meta, REDIS_LIVENESS_TTL_S)
            if self.user_id:
                users = _USER_RUNS_KEY.format(uid=self.user_id)
                await redis.zadd(users, {self.session_id: self.started_at})
                # Outlives any single run: the listing prunes members whose
                # liveness key is gone rather than trusting the index.
                await redis.expire(users, 3600)
        except Exception:  # noqa: BLE001, a mirroring fault must not stop the run
            logger.warning("run_manager: publish_start failed", exc_info=True)
            self._mirror_ok = False

    async def _mirror(self, event: AgentEvent) -> None:
        redis = await self._redis()
        if redis is None:
            return
        key = _STREAM_KEY.format(sid=self.session_id)
        try:
            await redis.xadd(
                key,
                {"event": event.event.value, "data": json.dumps(event.data, default=str)},
                maxlen=BUFFER_MAX,
                approximate=True,
            )
            if not self._stream_ttl_set:
                # Set as soon as the key exists rather than waiting for the
                # first heartbeat: a worker killed inside that window would
                # otherwise leave the log behind with no expiry at all.
                self._stream_ttl_set = True
                await redis.expire(key, REDIS_STREAM_TTL_S)
        except Exception:  # noqa: BLE001
            # Give up mirroring rather than logging per token. The run carries
            # on; only cross-worker attach is lost, and the local buffer still
            # serves attaches that land on this worker.
            logger.warning(
                "run_manager: mirror failed for %s, disabling", self.session_id,
                exc_info=True,
            )
            self._mirror_ok = False

    async def _heartbeat(self) -> None:
        """Keep the run's Redis keys alive while it is.

        Their TTL is how another worker tells a live run from one whose worker
        died mid-turn, so it has to be refreshed rather than set once.
        """
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_S)
                redis = await self._redis()
                if redis is None:
                    continue
                try:
                    await redis.expire(
                        _META_KEY.format(sid=self.session_id), REDIS_LIVENESS_TTL_S
                    )
                    await redis.expire(
                        _STREAM_KEY.format(sid=self.session_id), REDIS_STREAM_TTL_S
                    )
                except Exception:  # noqa: BLE001
                    pass
        except asyncio.CancelledError:
            pass

    async def _mark_finished_in_redis(self) -> None:
        redis = await self._redis()
        if redis is None:
            return
        try:
            meta = _META_KEY.format(sid=self.session_id)
            await redis.hset(meta, mapping={
                "status": "finished",
                "finished_at": str(self.finished_at),
            })
            # Outlive the run by the retention window, then vanish on their own.
            await redis.expire(meta, FINISHED_RETENTION_S)
            await redis.expire(_STREAM_KEY.format(sid=self.session_id), FINISHED_RETENTION_S)
            if self.user_id:
                await redis.zrem(_USER_RUNS_KEY.format(uid=self.user_id), self.session_id)
        except Exception:  # noqa: BLE001
            logger.warning("run_manager: mark finished failed", exc_info=True)


# ── Queue helpers ───────────────────────────────────────────────


def _offer(queue: asyncio.Queue, item: Any) -> bool:
    try:
        queue.put_nowait(item)
        return True
    except asyncio.QueueFull:
        return False


def _poison(queue: asyncio.Queue) -> None:
    """End a subscriber's iteration even when its queue is full.

    A subscriber is dropped precisely because its queue filled up, so the
    end-of-stream marker cannot simply be appended: it would be refused and the
    client would sit on keepalives forever waiting for a terminator that never
    arrives. Discard from the front to make room for it.
    """
    while True:
        try:
            queue.put_nowait(_EOS)
            return
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return


async def _drain(queue: asyncio.Queue) -> AsyncIterator[AgentEvent]:
    """Yield events until end of stream, filling silence with keepalives."""
    while True:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
        except asyncio.TimeoutError:
            yield AgentEvent(event=AgentEventType.KEEPALIVE, data={})
            continue
        if item is _EOS:
            return
        yield item


# ── Registry ────────────────────────────────────────────────────


def _sweep() -> None:
    """Forget runs past their retention window."""
    for session_id in [sid for sid, run in _runs.items() if run.is_expired]:
        _runs.pop(session_id, None)


def get_local_run(session_id: str) -> AgentRun | None:
    run = _runs.get(session_id)
    if run is not None and run.is_expired:
        _runs.pop(session_id, None)
        return None
    return run


async def start_run(
    agent,
    message: str,
    session,
    image_blocks: list[dict] | None = None,
    model_override: str | None = None,
) -> AgentRun:
    """Begin a detached run for `session`, or refuse if one is already live.

    Raises:
        RunAlreadyActive: a run for this session is still going. Refusing is
            the point: the caller should attach to it instead of racing it.
    """
    _sweep()

    existing = get_local_run(session.session_id)
    if existing is not None and existing.is_running:
        raise RunAlreadyActive(session.session_id, existing.run_id)

    remote = await _read_remote_meta(session.session_id)
    if remote and remote.get("status") == "running":
        raise RunAlreadyActive(session.session_id, remote.get("run_id", ""))

    from app.core import stream_registry

    stream = AgentEventStream()
    run = AgentRun(
        session_id=session.session_id,
        user_id=getattr(session.auth, "user_id", "") if session.auth else "",
        agent_name=getattr(session, "agent_name", "") or "",
        stream=stream,
        app_code=(session.context or {}).get("app_code", "") if hasattr(session, "context") else "",
    )
    _runs[session.session_id] = run

    # Control signals (/stop, /confirm) address the stream, wherever they land.
    stream_registry.register(session.session_id, stream)

    await run.publish_start()
    run.start(agent, message, session, image_blocks, model_override)
    return run


async def _read_remote_meta(session_id: str) -> dict[str, str] | None:
    from app.services.redis_client import get_redis_client

    redis = await get_redis_client()
    if redis is None:
        return None
    try:
        meta = await redis.hgetall(_META_KEY.format(sid=session_id))
        return meta or None
    except Exception:  # noqa: BLE001
        logger.warning("run_manager: meta read failed for %s", session_id, exc_info=True)
        return None


async def list_live_runs(user_id: str, agent_name: str = "") -> list[dict[str, Any]]:
    """Every run still going for this user, across workers.

    What the client asks on load, so a refresh can rejoin a turn without the
    user having to remember which chat was mid-answer.
    """
    _sweep()
    found: dict[str, dict[str, Any]] = {}

    for run in _runs.values():
        if run.is_running and run.user_id == user_id:
            if not agent_name or run.agent_name == agent_name:
                found[run.session_id] = run.describe()

    from app.services.redis_client import get_redis_client

    redis = await get_redis_client()
    if redis is not None:
        try:
            session_ids = await redis.zrange(_USER_RUNS_KEY.format(uid=user_id), 0, -1)
            for session_id in session_ids:
                if session_id in found:
                    continue
                meta = await _read_remote_meta(session_id)
                if not meta or meta.get("status") != "running":
                    # The key expired with the worker that held it: the run is
                    # gone, whatever the index still claims.
                    await redis.zrem(_USER_RUNS_KEY.format(uid=user_id), session_id)
                    continue
                if agent_name and meta.get("agent_name") != agent_name:
                    continue
                found[session_id] = {
                    "session_id": session_id,
                    "run_id": meta.get("run_id", ""),
                    "agent_name": meta.get("agent_name", ""),
                    "app_code": meta.get("app_code", ""),
                    "status": "running",
                    "started_at": float(meta.get("started_at") or 0),
                    "remote": True,
                }
        except Exception:  # noqa: BLE001
            logger.warning("run_manager: live run listing failed", exc_info=True)

    return sorted(found.values(), key=lambda r: r.get("started_at") or 0, reverse=True)


async def subscribe(session_id: str) -> AsyncIterator[AgentEvent] | None:
    """Attach to a run, from this worker or a sibling's Redis mirror.

    Returns None when there is no run to attach to, which the caller turns into
    a 404 so the client falls back to plain history.
    """
    run = get_local_run(session_id)
    if run is not None:
        return run.subscribe()

    meta = await _read_remote_meta(session_id)
    if not meta:
        return None
    return _subscribe_remote(session_id, meta)


async def _subscribe_remote(
    session_id: str, meta: dict[str, str]
) -> AsyncIterator[AgentEvent]:
    """Serve an attach for a run held by another worker, off the Redis stream.

    Same shape as the local path: replay what the stream holds, then tail it.
    Ends on the run's own done event, and on the meta key disappearing, which is
    is a worker that died mid-turn, and the client is told rather than left on
    a spinner.
    """
    from app.services.redis_client import get_redis_client

    redis = await get_redis_client()
    if redis is None:
        return

    key = _STREAM_KEY.format(sid=session_id)
    run_id = meta.get("run_id", "")
    running = meta.get("status") == "running"

    entries = await redis.xrange(key)
    last_id = entries[-1][0] if entries else "0-0"
    replayed = [_event_from_redis(fields) for _, fields in entries]
    replayed = [event for event in replayed if event is not None]

    yield AgentEvent(
        event=AgentEventType.REPLAY_START,
        data={"run_id": run_id, "session_id": session_id, "count": len(replayed),
              "remote": True},
    )
    saw_done = False
    for event in replayed:
        if event.event == AgentEventType.DONE:
            saw_done = True
        yield event
    yield AgentEvent(
        event=AgentEventType.REPLAY_END,
        data={"run_id": run_id, "session_id": session_id,
              "running": running and not saw_done, "remote": True},
    )

    if saw_done or not running:
        return

    while True:
        try:
            response = await redis.xread(
                {key: last_id}, count=200, block=int(KEEPALIVE_S * 1000)
            )
        except Exception:  # noqa: BLE001
            logger.warning("run_manager: remote tail failed for %s", session_id, exc_info=True)
            yield AgentEvent(
                event=AgentEventType.ERROR,
                data={"message": "Lost contact with the running agent. Reopen the chat to reconnect."},
            )
            return

        if not response:
            # Nothing in a keepalive window. Either genuinely quiet, or the
            # worker holding the run is gone. The meta key answers which.
            still_there = await _read_remote_meta(session_id)
            if not still_there:
                yield AgentEvent(
                    event=AgentEventType.ERROR,
                    data={"message": "The agent stopped unexpectedly."},
                )
                yield AgentEvent(
                    event=AgentEventType.DONE, data={"session_id": session_id},
                )
                return
            yield AgentEvent(event=AgentEventType.KEEPALIVE, data={})
            continue

        for _stream_key, items in response:
            for entry_id, fields in items:
                last_id = entry_id
                event = _event_from_redis(fields)
                if event is None:
                    continue
                yield event
                if event.event == AgentEventType.DONE:
                    return


def _event_from_redis(fields: dict[str, str]) -> AgentEvent | None:
    try:
        return AgentEvent(
            event=AgentEventType(fields["event"]),
            data=json.loads(fields.get("data") or "{}"),
        )
    except Exception:  # noqa: BLE001, one bad entry must not end the stream
        logger.warning("run_manager: undecodable mirrored event", exc_info=True)
        return None


async def shutdown() -> None:
    """Cancel every local run. Called from the app lifespan."""
    for run in list(_runs.values()):
        run.cancel()
    _runs.clear()
