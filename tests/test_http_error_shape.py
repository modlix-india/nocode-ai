"""Every HTTP error must be readable by the platform's own toast.

Measured 2026-09-18, and the finding is worth stating plainly: **every HTTP
error this service had ever returned rendered as a completely blank toast.**

The chain, end to end:

  nocode-ui `SendData` emits `error.data = res.data` — the parsed response body.
  A page does `UIEngine.Message(msg = Steps.call.error.data)`.
  `Messages.tsx:103` renders `isObject ? e.msg.message : e.msg`.

So an object body is rendered by reading `.message`. Every Spring service in
Modlix answers with a flat `{message, debugMessage, exceptionId, stackTrace}`,
which is what that line was written for. FastAPI's default is `{"detail": ...}`,
which has no `message` — so the toast showed an icon and an empty box.

Nothing caught it because the agent surfaces are SSE: their errors arrive as
stream events and never touch the HTTP error path. The blueprint routes are the
first plain HTTP ones somebody presses a button to reach, and the button that
exposed it was Generate against an empty wallet — a real 402 carrying a real
sentence, displayed as nothing at all.

These tests pin the flat `message`, and pin that `detail` is still there so no
existing reader of it breaks.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.main import http_error


def _app() -> TestClient:
    """A bare app carrying only the handler under test.

    Not `app.main.app`: importing that builds the whole service. The handler is
    a pure function of the exception and is registered the same way here.
    """
    from starlette.exceptions import HTTPException as StarletteHTTPException

    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, http_error)

    @app.get("/text")
    async def text():
        raise HTTPException(status_code=402, detail="You're out of tokens.")

    @app.get("/structured")
    async def structured():
        raise HTTPException(status_code=400, detail={
            "message": "The plan was refused. Nothing was written.",
            "issues": [{"path": "plan.objects", "message": "Arrays are not allowed."}],
        })

    @app.get("/nameless")
    async def nameless():
        raise HTTPException(status_code=400, detail={"issues": ["something"]})

    return TestClient(app, raise_server_exceptions=False)


def test_a_plain_message_is_readable_by_the_toast():
    body = _app().get("/text").json()
    # The line the renderer actually reads.
    assert body["message"] == "You're out of tokens."
    # And the old key, unchanged, so nothing reading `detail` breaks.
    assert body["detail"] == "You're out of tokens."


def test_a_structured_refusal_keeps_its_own_message():
    response = _app().get("/structured")
    body = response.json()
    assert response.status_code == 400
    assert body["message"] == "The plan was refused. Nothing was written."
    # The parts a caller needs are flat too, so a page can show the issues
    # without reaching through `detail`.
    assert body["issues"][0]["path"] == "plan.objects"
    assert body["detail"]["message"] == "The plan was refused. Nothing was written."


def test_a_structured_error_with_no_message_still_says_something():
    # A blank toast is the failure this whole module exists for, so the one
    # thing that must never happen is an object body with no `message` at all.
    body = _app().get("/nameless").json()
    assert body["message"]
    assert body["issues"] == ["something"]


def test_the_status_code_is_untouched():
    # 402 is load-bearing: it is how a client tells "add money" from "retry",
    # and a handler that flattened everything to 500 would erase that.
    assert _app().get("/text").status_code == 402
    assert _app().get("/structured").status_code == 400


def test_a_route_that_does_not_exist_is_still_readable():
    # Starlette raises its own HTTPException for a 404 with no route, which is
    # why the handler is registered against StarletteHTTPException rather than
    # FastAPI's subclass: registering the subclass leaves these unhandled.
    response = _app().get("/nothing-here")
    assert response.status_code == 404
    assert response.json()["message"]
