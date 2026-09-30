"""The cross-worker half of a detached run.

Production runs four uvicorn workers behind one gunicorn master with no
affinity, so an attach lands on the worker holding the run about one time in
four. The other three are served from the Redis mirror by
``run_manager._subscribe_remote``, and that path has no in-process fallback to
hide a mistake: get it wrong and three quarters of reattaches hang.

These tests need a reachable Redis and skip without one, so they cover the
local and dev machines (where redis.url resolves) and stay out of the way
anywhere it does not.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.config import settings
from app.core import run_manager, stream_registry
from app.core.streaming import AgentEventType


class FakeAuth:
    user_id = "mirror-user"
    client_code = "SYSTEM"


class FakeSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.auth = FakeAuth()
        self.agent_name = "mirrortest"
        self.context: dict = {"app_code": "testapp"}

    async def persist_turn(self, *a, **kw):
        pass

    async def complete(self):
        pass


class ScriptedAgent:
    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.gate = gate

    async def run(self, message, session, event_stream, image_blocks=None, model_override=None):
        await event_stream.emit_text("Hel")
        await event_stream.emit_text("lo")
        if self.gate is not None:
            await self.gate.wait()
        await event_stream.emit_tool_result("add_component", True, "Added", "t1")


async def _redis_or_skip():
    from app.services.redis_client import get_redis_client

    if not settings.REDIS_URL:
        # Nothing configured (no config server on this machine).
        pytest.skip("no REDIS_URL configured")
    settings.REDIS_ENABLED = True
    redis = await get_redis_client()
    if redis is None:
        pytest.skip("Redis not reachable")
    return redis


@pytest.fixture
def session_id():
    # Distinct per run so a leftover key from a previous run cannot pass a test.
    import uuid

    return f"mirrortest_{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def clean():
    run_manager._runs.clear()
    stream_registry._local_streams.clear()
    yield
    run_manager._runs.clear()
    stream_registry._local_streams.clear()


@pytest.mark.asyncio
async def test_the_mirror_carries_the_turn_and_its_liveness(session_id):
    redis = await _redis_or_skip()
    try:
        run = await run_manager.start_run(ScriptedAgent(), "hi", FakeSession(session_id))
        await asyncio.wait_for(run._pump_task, timeout=5)

        meta = await redis.hgetall(f"ai:run:{session_id}:meta")
        assert meta["status"] == "finished"
        assert meta["run_id"] == run.run_id
        assert meta["user_id"] == "mirror-user"

        entries = await redis.xrange(f"ai:run:{session_id}")
        kinds = [fields["event"] for _, fields in entries]
        assert kinds[-1] == AgentEventType.DONE.value
        # Unlike the in-process buffer, the mirror keeps every delta: it is a
        # log, not a snapshot, and a tailing reader needs them in order.
        text = "".join(
            json.loads(f["data"])["text"] for _, f in entries if f["event"] == "text"
        )
        assert text == "Hello"

        # Both keys carry a TTL. Without one a killed worker would leave its
        # run behind forever, and a stale "running" marker refuses new
        # messages on that session.
        assert 0 < await redis.ttl(f"ai:run:{session_id}") <= run_manager.REDIS_STREAM_TTL_S
        assert 0 < await redis.ttl(f"ai:run:{session_id}:meta") <= run_manager.FINISHED_RETENTION_S
    finally:
        await redis.delete(f"ai:run:{session_id}", f"ai:run:{session_id}:meta")
        await redis.zrem("ai:runs:user:mirror-user", session_id)


@pytest.mark.asyncio
async def test_a_worker_with_no_local_run_serves_the_attach_from_redis(session_id):
    """The one-in-four case, forced by forgetting the run locally."""
    redis = await _redis_or_skip()
    try:
        run = await run_manager.start_run(ScriptedAgent(), "hi", FakeSession(session_id))
        await asyncio.wait_for(run._pump_task, timeout=5)

        # Stand in for a sibling worker: same Redis, no local run.
        run_manager._runs.clear()
        assert run_manager.get_local_run(session_id) is None

        events = await run_manager.subscribe(session_id)
        assert events is not None, "a mirrored run must still be attachable"

        got = [event async for event in events]
        kinds = [e.event for e in got]
        assert kinds[0] == AgentEventType.REPLAY_START
        assert got[0].data["remote"] is True
        assert AgentEventType.DONE in kinds
        assert kinds[-1] == AgentEventType.REPLAY_END
        assert got[-1].data["running"] is False

        text = "".join(e.data["text"] for e in got if e.event == AgentEventType.TEXT)
        assert text == "Hello"
    finally:
        await redis.delete(f"ai:run:{session_id}", f"ai:run:{session_id}:meta")
        await redis.zrem("ai:runs:user:mirror-user", session_id)


@pytest.mark.asyncio
async def test_a_run_on_a_sibling_worker_is_listed_and_refuses_a_second_send(session_id):
    redis = await _redis_or_skip()
    gate = asyncio.Event()
    try:
        await run_manager.start_run(ScriptedAgent(gate), "hi", FakeSession(session_id))
        run_manager._runs.clear()  # as if the run belonged to another worker

        listed = await run_manager.list_live_runs("mirror-user", agent_name="mirrortest")
        assert [r["session_id"] for r in listed] == [session_id]
        assert listed[0]["remote"] is True

        # And the session is not open for a second agent, whichever worker the
        # send lands on.
        with pytest.raises(run_manager.RunAlreadyActive):
            await run_manager.start_run(ScriptedAgent(), "again", FakeSession(session_id))
    finally:
        gate.set()
        await asyncio.sleep(0.05)
        await redis.delete(f"ai:run:{session_id}", f"ai:run:{session_id}:meta")
        await redis.zrem("ai:runs:user:mirror-user", session_id)


@pytest.mark.asyncio
async def test_a_previous_turns_events_are_not_replayed_into_the_next(session_id):
    """Each run starts its log clean, or a reattach shows the last answer."""
    redis = await _redis_or_skip()
    try:
        first = await run_manager.start_run(ScriptedAgent(), "hi", FakeSession(session_id))
        await asyncio.wait_for(first._pump_task, timeout=5)
        first_len = len(await redis.xrange(f"ai:run:{session_id}"))
        assert first_len > 0

        run_manager._runs.clear()
        second = await run_manager.start_run(ScriptedAgent(), "again", FakeSession(session_id))
        await asyncio.wait_for(second._pump_task, timeout=5)

        entries = await redis.xrange(f"ai:run:{session_id}")
        text = "".join(
            json.loads(f["data"])["text"] for _, f in entries if f["event"] == "text"
        )
        assert text == "Hello", "the second turn's log still holds the first turn's text"
    finally:
        await redis.delete(f"ai:run:{session_id}", f"ai:run:{session_id}:meta")
        await redis.zrem("ai:runs:user:mirror-user", session_id)
