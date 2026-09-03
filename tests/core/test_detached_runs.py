"""A run outlives the client that started it.

The behaviour under test is the one that used to be broken: a disconnecting
SSE consumer cancelled the agent task mid-tool, and ``BaseAgent.run``'s
CancelledError path recorded the turn as "[Stopped by user]". Closing the
sidekick panel, switching sessions or refreshing therefore destroyed work.

Redis is off in these tests (``REDIS_ENABLED`` defaults false), so everything
here exercises the in-process path, the same one a local single-worker run
takes. The cross-worker mirror is covered separately by inspection of
``_subscribe_remote``, which needs a live Redis.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core import run_manager, stream_registry
from app.core.run_manager import AgentRun
from app.core.streaming import AgentEventStream, AgentEventType


class FakeAuth:
    user_id = "u1"
    client_code = "SYSTEM"


class FakeSession:
    """Only the surface run_manager touches."""

    def __init__(self, session_id: str = "s1") -> None:
        self.session_id = session_id
        self.auth = FakeAuth()
        self.agent_name = "appbuilder"
        self.context: dict = {"app_code": "testapp"}
        self.persisted: list[tuple[str, str]] = []

    async def persist_turn(self, user_text, assistant_summary, tool_calls=None, model=None):
        self.persisted.append((user_text, assistant_summary))

    async def complete(self):
        pass


class ScriptedAgent:
    """Emits a fixed script, pausing where the test wants to interleave."""

    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.gate = gate
        self.finished = False
        self.saw_cancel = False

    async def run(self, message, session, event_stream, image_blocks=None, model_override=None):
        try:
            await event_stream.emit_text("Hel")
            await event_stream.emit_text("lo")
            if self.gate is not None:
                await self.gate.wait()
            await event_stream.emit_tool_result("add_component", True, "Added", "t1")
            self.finished = True
        except asyncio.CancelledError:
            self.saw_cancel = True
            raise


async def collect(events, stop_after_done: bool = True) -> list:
    out = []
    async for event in events:
        if event.event == AgentEventType.KEEPALIVE:
            continue
        out.append(event)
        if stop_after_done and event.event == AgentEventType.DONE:
            break
    return out


@pytest.fixture(autouse=True)
def clean_registry():
    run_manager._runs.clear()
    stream_registry._local_streams.clear()
    yield
    run_manager._runs.clear()
    stream_registry._local_streams.clear()


@pytest.mark.asyncio
async def test_run_survives_a_disconnecting_subscriber():
    """The whole point: dropping the stream must not touch the agent."""
    gate = asyncio.Event()
    agent = ScriptedAgent(gate)
    session = FakeSession()

    run = await run_manager.start_run(agent, "build me a page", session)

    # A client attaches, reads one event, then vanishes mid-run.
    events = run.subscribe()
    first = await events.__anext__()
    assert first.event == AgentEventType.REPLAY_START
    await events.aclose()

    # Nothing is listening. The agent carries on regardless.
    gate.set()
    await asyncio.wait_for(run._agent_task, timeout=2)

    assert agent.finished is True
    assert agent.saw_cancel is False
    assert session.persisted == []  # nothing wrote "[Stopped by user]"


@pytest.mark.asyncio
async def test_reattach_replays_the_turn_then_follows_it_live():
    gate = asyncio.Event()
    run = await run_manager.start_run(ScriptedAgent(gate), "hi", FakeSession())

    # Let the first two text deltas land before anyone attaches.
    await asyncio.sleep(0.05)

    events = await run_manager.subscribe("s1")
    assert events is not None

    replayed = []
    async for event in events:
        replayed.append(event)
        if event.event == AgentEventType.REPLAY_END:
            break

    assert replayed[0].event == AgentEventType.REPLAY_START
    assert replayed[-1].event == AgentEventType.REPLAY_END
    assert replayed[-1].data["running"] is True
    # Both deltas are present, coalesced into one entry.
    texts = [e.data["text"] for e in replayed if e.event == AgentEventType.TEXT]
    assert texts == ["Hello"]

    # And the rest of the run arrives live on the same subscription.
    gate.set()
    rest = await asyncio.wait_for(collect(events), timeout=2)
    kinds = [e.event for e in rest]
    assert AgentEventType.TOOL_RESULT in kinds
    assert kinds[-1] == AgentEventType.DONE


@pytest.mark.asyncio
async def test_attaching_after_the_run_ends_replays_the_whole_turn():
    run = await run_manager.start_run(ScriptedAgent(), "hi", FakeSession())
    await asyncio.wait_for(run._pump_task, timeout=2)
    assert run.status == "finished"

    events = await run_manager.subscribe("s1")
    assert events is not None
    got = await asyncio.wait_for(collect(events, stop_after_done=False), timeout=2)

    kinds = [e.event for e in got]
    assert kinds[0] == AgentEventType.REPLAY_START
    # The turn's own done event is part of what gets replayed, so it arrives
    # INSIDE the bracket and replay_end is last. A client must therefore treat
    # done-during-replay as the end of the turn, and read replay_end.running
    # as the authority on whether anything is still coming.
    assert AgentEventType.DONE in kinds
    assert kinds[-1] == AgentEventType.REPLAY_END
    assert got[-1].data["running"] is False


@pytest.mark.asyncio
async def test_two_clients_watch_the_same_run():
    """A session open in two places must not need two runs."""
    gate = asyncio.Event()
    run = await run_manager.start_run(ScriptedAgent(gate), "hi", FakeSession())

    a = run.subscribe()
    b = run.subscribe()
    # Drain both past their replay so each holds a live subscription.
    for events in (a, b):
        async for event in events:
            if event.event == AgentEventType.REPLAY_END:
                break

    gate.set()
    got_a = await asyncio.wait_for(collect(a), timeout=2)
    got_b = await asyncio.wait_for(collect(b), timeout=2)

    assert [e.event for e in got_a] == [e.event for e in got_b]


@pytest.mark.asyncio
async def test_a_second_send_on_a_live_session_is_refused():
    gate = asyncio.Event()
    session = FakeSession()
    await run_manager.start_run(ScriptedAgent(gate), "first", session)

    with pytest.raises(run_manager.RunAlreadyActive):
        await run_manager.start_run(ScriptedAgent(), "second", FakeSession())

    gate.set()


@pytest.mark.asyncio
async def test_send_is_allowed_once_the_previous_run_finished():
    run = await run_manager.start_run(ScriptedAgent(), "first", FakeSession())
    await asyncio.wait_for(run._pump_task, timeout=2)

    second = await run_manager.start_run(ScriptedAgent(), "second", FakeSession())
    assert second.run_id != run.run_id


@pytest.mark.asyncio
async def test_stop_is_the_only_thing_that_ends_a_run_early():
    class Looping:
        def __init__(self):
            self.stopped_cleanly = False

        async def run(self, message, session, event_stream, image_blocks=None, model_override=None):
            for _ in range(1000):
                if event_stream.is_cancelled:
                    self.stopped_cleanly = True
                    return
                await event_stream.emit_text(".")
                await asyncio.sleep(0.005)

    agent = Looping()
    run = await run_manager.start_run(agent, "loop", FakeSession())
    await asyncio.sleep(0.05)

    delivered = await stream_registry.signal("s1", "stop")
    assert delivered == "local"

    await asyncio.wait_for(run._agent_task, timeout=2)
    assert agent.stopped_cleanly is True
    assert run.stream.is_cancelled is True


@pytest.mark.asyncio
async def test_a_crashing_agent_still_terminates_its_subscribers():
    class Exploding:
        async def run(self, message, session, event_stream, image_blocks=None, model_override=None):
            await event_stream.emit_text("starting")
            raise RuntimeError("tool blew up")

    run = await run_manager.start_run(Exploding(), "hi", FakeSession())
    got = await asyncio.wait_for(collect(run.subscribe()), timeout=2)

    kinds = [e.event for e in got]
    assert AgentEventType.ERROR in kinds
    assert kinds[-1] == AgentEventType.DONE


@pytest.mark.asyncio
async def test_live_runs_are_listed_for_their_owner():
    gate = asyncio.Event()
    await run_manager.start_run(ScriptedAgent(gate), "hi", FakeSession("s1"))

    runs = await run_manager.list_live_runs("u1", agent_name="appbuilder")
    assert [r["session_id"] for r in runs] == ["s1"]
    assert runs[0]["app_code"] == "testapp"

    assert await run_manager.list_live_runs("someone-else") == []
    assert await run_manager.list_live_runs("u1", agent_name="adzump") == []

    gate.set()


@pytest.mark.asyncio
async def test_attach_to_an_unknown_session_is_a_miss():
    assert await run_manager.subscribe("never-existed") is None


@pytest.mark.asyncio
async def test_text_coalescing_keeps_the_replay_bounded():
    stream = AgentEventStream()
    run = AgentRun("s9", "u1", "appbuilder", stream)
    for i in range(500):
        run._append(await _text(i))

    assert len(run._buffer) == 1
    assert run._buffer[0].data["text"] == "".join(str(i % 10) for i in range(500))


async def _text(i: int):
    from app.core.streaming import AgentEvent

    return AgentEvent(event=AgentEventType.TEXT, data={"text": str(i % 10), "agent_id": "root"})


@pytest.mark.asyncio
async def test_coalescing_does_not_mutate_what_a_live_client_received():
    """The buffer merges into its own copy, never into a delivered event."""
    stream = AgentEventStream()
    run = AgentRun("s9", "u1", "appbuilder", stream)

    first = await _text(1)
    run._append(first)
    run._append(await _text(2))

    assert first.data["text"] == "1"
    assert run._buffer[0].data["text"] == "12"


@pytest.mark.asyncio
async def test_keepalives_are_never_buffered():
    from app.core.streaming import AgentEvent

    run = AgentRun("s9", "u1", "appbuilder", AgentEventStream())
    run._append(AgentEvent(event=AgentEventType.KEEPALIVE, data={}))
    assert len(run._buffer) == 0


@pytest.mark.asyncio
async def test_finished_runs_stop_answering_control_signals():
    run = await run_manager.start_run(ScriptedAgent(), "hi", FakeSession())
    await asyncio.wait_for(run._pump_task, timeout=2)

    # The stream is deregistered when the run ends, so /stop reports honestly
    # instead of claiming it delivered a signal to a dead run.
    assert await stream_registry.signal("s1", "stop") == "missing"
