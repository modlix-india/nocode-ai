"""Where a write goes when the user has the object open.

Before 2026-09-03 the answer was always "the browser": a write to any declared
object was held and streamed back as a patch. That bought review at three
prices. The change was not in the database, so `screenshot_page` could not see
it and the agent looking at its own work saw nothing. It lived in one tab. And
every part-of-an-object endpoint had to be re-implemented against a copy.

Now the answer depends on whether the SERVER can draft the object:

    draftable     -> the server's draft, and the tab refetches it
    not draftable -> held in the browser, as before

These tests pin both halves, and the two ways to get the split wrong. Sending a
non-draftable kind to the draft surface writes it LIVE while the agent reports
it as pending. Holding a draftable one keeps it out of the database, where a
screenshot cannot find it.
"""

from __future__ import annotations

import pytest

from app.core.tools import draft_registry as drafts
from app.core.tools.draft_registry import DraftEntry, DraftRegistry, open_drafts
from app.core.tools.http_client import SaasClient


@pytest.fixture(autouse=True)
def _no_drafting():
    """Default off, so a test that forgets to opt in gets the old behaviour."""
    token = drafts.drafting.set(False)
    yield
    drafts.drafting.reset(token)


@pytest.fixture
def drafting_on():
    token = drafts.drafting.set(True)
    yield
    drafts.drafting.reset(token)


def _registry(kind: str, obj_id: str, api_name: str = "thing") -> DraftRegistry:
    reg = DraftRegistry(session_id="s1")
    reg.declare(
        DraftEntry(
            kind=kind, id=obj_id, name=api_name, app_code="monkbars",
            doc={"id": obj_id, "name": api_name, "appCode": "monkbars"},
            loaded=True,
        )
    )
    return reg


def _recording_client(monkeypatch, payload=None):
    """A SaasClient that records the request it would have sent."""
    client = SaasClient("http://unreachable.invalid")
    sent: list[dict] = []

    class _Response:
        status_code = 200
        content = b"{}"
        headers = {"content-type": "application/json"}
        text = ""

        @staticmethod
        def json():
            return payload if payload is not None else {"id": "x"}

    class _Http:
        @staticmethod
        async def request(**kwargs):
            sent.append(kwargs)
            return _Response()

    monkeypatch.setattr(client, "_get_client", lambda: _Http())
    return client, sent


# ── The split ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", sorted(drafts.DRAFTABLE_KINDS))
def test_every_definition_kind_is_draftable(kind):
    assert drafts.is_draftable(kind) is True


@pytest.mark.parametrize("kind", ["profile", "role", "user", "client", "department", "designation"])
def test_no_security_kind_is_draftable(kind):
    """The org console's objects have no Draft row available at any price."""
    assert drafts.is_draftable(kind) is False
    # But they still resolve, or they could be neither held nor reported.
    assert kind in drafts.PATH_KINDS.values()


@pytest.mark.asyncio
async def test_a_draftable_open_object_is_not_held(drafting_on):
    token = open_drafts.set(_registry("storage", "s1"))
    try:
        client = SaasClient("http://unreachable.invalid")
        held = await client._serve_from_draft("PUT", "/api/core/storages/s1", {"id": "s1"})
    finally:
        open_drafts.reset(token)
    assert held is None, "it must reach the server, which drafts it"


@pytest.mark.asyncio
async def test_a_non_draftable_open_object_is_still_held(drafting_on):
    """A profile has no draft surface, so the browser is the only review step."""
    token = open_drafts.set(_registry("profile", "pr1"))
    try:
        client = SaasClient("http://unreachable.invalid")
        held = await client._serve_from_draft(
            "PUT", "/api/security/profiles/pr1", {"id": "pr1", "name": "edited"}
        )
    finally:
        open_drafts.reset(token)

    assert held is not None and held.success
    assert "not saved" in held.summary


@pytest.mark.asyncio
async def test_a_draftable_object_is_held_when_the_deployment_cannot_draft():
    """Drafting off is the safe fallback, not a licence to write live.

    `drafting` is false both when the caller did not ask and when the
    deployment failed its probe, and the two must behave identically: hold it
    in the browser rather than saving something the user was told was pending.
    """
    token = open_drafts.set(_registry("storage", "s1"))
    try:
        client = SaasClient("http://unreachable.invalid")
        held = await client._serve_from_draft("PUT", "/api/core/storages/s1", {"id": "s1"})
    finally:
        open_drafts.reset(token)
    assert held is not None and held.success


# ── The flag on the wire ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_whole_object_put_carries_the_draft_flag(monkeypatch, drafting_on):
    client, sent = _recording_client(monkeypatch)
    await client.put("/api/core/storages/s1", json={"id": "s1"})
    assert sent[0]["params"] == {"draft": "true"}


@pytest.mark.asyncio
async def test_a_read_of_one_object_follows_the_same_surface(monkeypatch, drafting_on):
    """Reading live after drafting shows the agent the version it replaced."""
    client, sent = _recording_client(monkeypatch)
    await client.get("/api/ui/themes/t1")
    assert sent[0]["params"] == {"draft": "true"}


@pytest.mark.asyncio
async def test_a_collection_listing_is_left_alone(monkeypatch, drafting_on):
    """The draft surface has no opinion about lists, so claiming one is a lie."""
    client, sent = _recording_client(monkeypatch)
    await client.get("/api/ui/pages", params={"appCode": "monkbars"})
    assert sent[0]["params"] == {"appCode": "monkbars"}


@pytest.mark.asyncio
async def test_creating_is_never_drafted(monkeypatch, drafting_on):
    """A Draft row keyed on a name with no live document has nothing to publish."""
    client, sent = _recording_client(monkeypatch)
    await client.post("/api/core/storages", json={"name": "orders"})
    assert sent[0]["params"] is None


@pytest.mark.asyncio
async def test_a_security_write_is_never_drafted(monkeypatch, drafting_on):
    """Nothing in security drafts, so a flag there would be silently ignored."""
    client, sent = _recording_client(monkeypatch)
    await client.put("/api/security/profiles/pr1", json={"id": "pr1"})
    assert sent[0]["params"] is None


@pytest.mark.asyncio
async def test_nothing_is_flagged_when_the_turn_is_not_drafting(monkeypatch):
    client, sent = _recording_client(monkeypatch)
    await client.put("/api/core/storages/s1", json={"id": "s1"})
    assert sent[0]["params"] is None


@pytest.mark.asyncio
async def test_an_explicit_flag_from_a_tool_is_not_overwritten(monkeypatch, drafting_on):
    client, sent = _recording_client(monkeypatch)
    await client.put("/api/ui/pages/p1", json={"id": "p1"}, params={"draft": "true"})
    assert sent[0]["params"] == {"draft": "true"}


# ── The live-only endpoints ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_component_patch_is_refused_while_drafting(monkeypatch, drafting_on):
    """`/components/{key}` takes no draft flag: it would publish immediately."""
    client, sent = _recording_client(monkeypatch)
    r = await client.patch(
        "/api/ui/pages/p1/components/btn", json={"componentData": {"key": "btn"}}
    )
    assert r.success is False
    assert "no draft counterpart" in (r.error or "")
    assert sent == [], "it must not reach the network at all"


@pytest.mark.asyncio
async def test_an_event_put_is_refused_while_drafting(monkeypatch, drafting_on):
    client, sent = _recording_client(monkeypatch)
    r = await client.put("/api/ui/pages/p1/events/onClick", json={"definition": {}})
    assert r.success is False
    assert sent == []


@pytest.mark.asyncio
async def test_the_same_component_patch_is_allowed_when_not_drafting(monkeypatch):
    client, sent = _recording_client(monkeypatch)
    r = await client.patch(
        "/api/ui/pages/p1/components/btn", json={"componentData": {"key": "btn"}}
    )
    assert r.success is True
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_reading_a_sub_resource_is_never_refused(monkeypatch, drafting_on):
    client, sent = _recording_client(monkeypatch)
    r = await client.get("/api/ui/pages/p1/events/onClick")
    assert r.success is True
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_a_security_sub_resource_write_is_not_refused(monkeypatch, drafting_on):
    """Only draftable kinds have a draft to be inconsistent with."""
    client, sent = _recording_client(monkeypatch)
    r = await client.post("/api/security/users/u1/removeRole", json={"roleId": "r1"})
    assert r.success is True
    assert len(sent) == 1


# ── What the client is told ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_drafted_write_is_announced_as_drafted(monkeypatch, drafting_on):
    """The tab has to know which surface to refetch, and cannot infer it."""
    told: list[dict] = []

    class _Stream:
        async def emit_object_changed(self, **kw):
            told.append(kw)

    reg = DraftRegistry(session_id="s1")
    reg.stream = _Stream()
    client, _sent = _recording_client(monkeypatch, payload={"id": "s1", "name": "orders"})

    token = open_drafts.set(reg)
    try:
        await client.put("/api/core/storages/s1", json={"id": "s1", "name": "orders"})
    finally:
        open_drafts.reset(token)

    assert told and told[0]["draft"] is True
    assert told[0]["kind"] == "storage"
    assert told[0]["obj_id"] == "s1"


@pytest.mark.asyncio
async def test_a_security_write_is_announced_as_live(monkeypatch, drafting_on):
    told: list[dict] = []

    class _Stream:
        async def emit_object_changed(self, **kw):
            told.append(kw)

    reg = DraftRegistry(session_id="s1")
    reg.stream = _Stream()
    client, _sent = _recording_client(monkeypatch, payload={"id": "d1"})

    token = open_drafts.set(reg)
    try:
        await client.put("/api/security/departments/d1", json={"id": "d1"})
    finally:
        open_drafts.reset(token)

    assert told and told[0]["draft"] is False
    assert told[0]["kind"] == "department"


# ── Writes the verb cannot see ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_mutating_get_can_still_be_announced():
    """Part of the security service mutates over GET.

    `/users/{id}/assignRole/{roleId}` changes the user and answers 200 to a GET,
    so the choke point's verb test cannot see it and the console showing that
    user was never told. The tool announces it by hand instead.
    """
    told: list[dict] = []

    class _Stream:
        async def emit_object_changed(self, **kw):
            told.append(kw)

    reg = DraftRegistry(session_id="s1")
    reg.stream = _Stream()

    token = open_drafts.set(reg)
    try:
        await drafts.announce_change(kind="user", obj_id="u7", name="kiran")
    finally:
        open_drafts.reset(token)

    assert told == [{
        "kind": "user", "obj_id": "u7", "name": "kiran",
        "app_code": "", "operation": "UPDATE", "draft": False,
    }]


@pytest.mark.asyncio
async def test_announcing_without_a_client_listening_is_a_no_op():
    """Headless runs have no stream, and must not blow up on the way past."""
    await drafts.announce_change(kind="user", obj_id="u7")


@pytest.mark.asyncio
async def test_announcing_nothing_is_refused_quietly():
    told: list[dict] = []

    class _Stream:
        async def emit_object_changed(self, **kw):
            told.append(kw)

    reg = DraftRegistry(session_id="s1")
    reg.stream = _Stream()
    token = open_drafts.set(reg)
    try:
        await drafts.announce_change(kind="", obj_id="u7")
    finally:
        open_drafts.reset(token)
    assert told == []
