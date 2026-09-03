"""The chat routes, driven at the ASGI level.

The behaviour that matters here cannot be reached through httpx: its
ASGITransport buffers the whole response and only reports `http.disconnect`
once the app has finished, so it can never model a client that walks away
mid-stream. That is precisely the case this change is about: Starlette's
StreamingResponse cancels its generator the moment a disconnect arrives, so
these tests speak ASGI directly and cut the client off after the first chunk.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import APIRouter, Depends, FastAPI

from app.core import run_manager, stream_registry
from app.core.base_auth import require_auth_context
from app.core.base_router import create_common_routes, stream_agent_response


class FakeAuth:
    user_id = "u1"
    client_code = "SYSTEM"
    app_code = "testapp"
    access_app_code = "appbuilder"


class FakeSession:
    def __init__(self, session_id: str = "s1") -> None:
        self.session_id = session_id
        self.auth = FakeAuth()
        self.agent_name = "testagent"
        self.context: dict = {"app_code": "testapp"}

    async def persist_turn(self, *a, **kw):
        pass

    async def complete(self):
        pass


class StoredSession:
    """What the session manager hands back for the ownership check."""

    def __init__(self, user_id: str = "u1") -> None:
        self.user_id = user_id
        self.session_id = "s1"


class FakeSessionManager:
    def __init__(self, owner: str | None = "u1") -> None:
        self.owner = owner

    async def get_session(self, session_id: str):
        if self.owner is None:
            return None
        return StoredSession(self.owner)


class GatedAgent:
    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.finished = False
        self.saw_cancel = False

    async def run(self, message, session, event_stream, image_blocks=None, model_override=None):
        try:
            await event_stream.emit_text("before")
            await self.gate.wait()
            await event_stream.emit_text("after")
            self.finished = True
        except asyncio.CancelledError:
            self.saw_cancel = True
            raise


def build_app(agent) -> FastAPI:
    router = APIRouter()
    create_common_routes(router, agent_name="testagent")

    @router.post("/chat")
    async def chat(auth=Depends(require_auth_context)):
        return await stream_agent_response(agent, "hello", FakeSession())

    app = FastAPI()
    app.include_router(router, prefix="/agent")
    app.dependency_overrides[require_auth_context] = lambda: FakeAuth()
    return app


def _scope(method: str, path: str) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }


async def call(app, method: str, path: str, body: dict | None = None, drop_after: int | None = None):
    """Drive one ASGI request. `drop_after` disconnects after N body chunks."""
    payload = json.dumps(body or {}).encode()
    sent_request = False
    disconnected = asyncio.Event()
    chunks: list[bytes] = []
    start: dict = {}

    async def receive():
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            start.update(message)
        elif message["type"] == "http.response.body":
            body_bytes = message.get("body") or b""
            if body_bytes:
                chunks.append(body_bytes)
            if drop_after is not None and len(chunks) >= drop_after:
                disconnected.set()
            if not message.get("more_body", False):
                disconnected.set()

    await app(_scope(method, path), receive, send)
    return start.get("status"), b"".join(chunks).decode()


def events_in(text: str) -> list[str]:
    return [line[len("event: "):] for line in text.splitlines() if line.startswith("event: ")]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    run_manager._runs.clear()
    stream_registry._local_streams.clear()
    monkeypatch.setattr(
        "app.core.base_router.get_session_manager", lambda: FakeSessionManager("u1")
    )
    yield
    run_manager._runs.clear()
    stream_registry._local_streams.clear()


@pytest.mark.asyncio
async def test_a_client_that_walks_away_does_not_take_the_run_with_it():
    agent = GatedAgent()
    app = build_app(agent)

    # Disconnect as soon as the first chunk lands, which is what closing the
    # panel or refreshing the page does.
    status, _ = await call(app, "POST", "/agent/chat", drop_after=1)
    assert status == 200

    run = run_manager.get_local_run("s1")
    assert run is not None
    assert run.is_running is True
    assert agent.saw_cancel is False

    # The agent finishes its work with nobody watching.
    agent.gate.set()
    await asyncio.wait_for(run._agent_task, timeout=2)
    assert agent.finished is True


@pytest.mark.asyncio
async def test_attach_after_a_disconnect_returns_the_whole_turn():
    agent = GatedAgent()
    app = build_app(agent)
    await call(app, "POST", "/agent/chat", drop_after=1)

    agent.gate.set()
    await asyncio.wait_for(run_manager.get_local_run("s1")._pump_task, timeout=2)

    status, text = await call(app, "POST", "/agent/attach", {"session_id": "s1"})
    assert status == 200
    kinds = events_in(text)
    assert kinds[0] == "replay_start"
    assert kinds[-1] == "replay_end"
    assert "done" in kinds
    # Nothing said before the disconnect is missing from the replay.
    assert '"text": "beforeafter"' in text


@pytest.mark.asyncio
async def test_a_live_run_refuses_a_second_send():
    agent = GatedAgent()
    app = build_app(agent)
    await call(app, "POST", "/agent/chat", drop_after=1)

    status, text = await call(app, "POST", "/agent/chat")
    assert status == 409
    assert "already in progress" in text

    agent.gate.set()


@pytest.mark.asyncio
async def test_attach_to_a_session_with_no_run_is_a_404():
    status, _ = await call(build_app(GatedAgent()), "POST", "/agent/attach", {"session_id": "s1"})
    assert status == 404


@pytest.mark.asyncio
async def test_attach_to_someone_elses_session_is_refused(monkeypatch):
    agent = GatedAgent()
    app = build_app(agent)
    await call(app, "POST", "/agent/chat", drop_after=1)

    monkeypatch.setattr(
        "app.core.base_router.get_session_manager", lambda: FakeSessionManager("someone-else")
    )
    status, _ = await call(app, "POST", "/agent/attach", {"session_id": "s1"})
    assert status == 403

    agent.gate.set()


@pytest.mark.asyncio
async def test_runs_lists_what_is_still_working():
    agent = GatedAgent()
    app = build_app(agent)

    status, text = await call(app, "GET", "/agent/runs")
    assert status == 200
    assert json.loads(text)["runs"] == []

    await call(app, "POST", "/agent/chat", drop_after=1)
    status, text = await call(app, "GET", "/agent/runs")
    runs = json.loads(text)["runs"]
    assert [r["session_id"] for r in runs] == ["s1"]
    assert runs[0]["status"] == "running"

    agent.gate.set()
    await asyncio.wait_for(run_manager.get_local_run("s1")._pump_task, timeout=2)
    status, text = await call(app, "GET", "/agent/runs")
    assert json.loads(text)["runs"] == []


@pytest.mark.asyncio
async def test_a_dropped_subscriber_is_not_left_in_the_fan_out():
    """A disconnect must unsubscribe, not just stop reading.

    Left registered, the run would keep pushing every event into a queue
    nobody drains: one abandoned tab per refresh, for the life of the run.
    """
    agent = GatedAgent()
    app = build_app(agent)
    await call(app, "POST", "/agent/chat", drop_after=1)

    run = run_manager.get_local_run("s1")
    # aclose() on the response generator runs the subscription's cleanup.
    for _ in range(20):
        if not run._subscribers:
            break
        await asyncio.sleep(0.01)
    assert run._subscribers == set()

    agent.gate.set()
