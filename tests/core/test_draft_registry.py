"""The open-draft intercept: what gets held, what goes to the database.

The rule these tests pin down is that a write is held for exactly the objects the
client declared open, and everything else behaves as it always did. Both halves
matter equally: a hold that leaks to the database defeats the review step, and a
pass-through that gets held silently loses the user's change.
"""

from __future__ import annotations

import pytest

from app.core.tools.draft_registry import (
    DraftEntry,
    DraftRegistry,
    open_drafts,
)
from app.core.tools.http_client import SaasClient


# ── resolve(): which object is this call about ───────────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/api/ui/pages", ("page", None, None)),
        ("/api/ui/pages/abc123", ("page", "abc123", None)),
        ("/api/ui/pages?appCode=x", ("page", None, None)),
        ("/api/ui/themes/t1/", ("theme", "t1", None)),
        ("/api/core/storages/s1", ("storage", "s1", None)),
        ("/api/core/eventDefinitions/e1", ("eventdefinition", "e1", None)),
        # Longest prefix wins, so the two function APIs stay distinct.
        ("/api/ui/functions/f1", ("function", "f1", None)),
        ("/api/core/functions/f1", ("serverfunction", "f1", None)),
    ],
)
def test_resolve_maps_object_paths(path, expected):
    assert DraftRegistry.resolve(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        # Actions, not object edits. Must be invisible to the mechanism, or an
        # execute payload lands in front of the override-save match.
        "/api/core/functions/execute",
        # Not in the table at all.
        "/api/multi/application",
        "/api/security/applications/property",
        "/api/ui/personalization/p1",
        "/api/files/static/copyToClientPage",
    ],
)
def test_resolve_ignores_everything_else(path):
    assert DraftRegistry.resolve(path) == (None, None, None)


# ── The intercept ────────────────────────────────────────────────────────────


@pytest.fixture
def page_open():
    """One page declared open, with a token in it we can watch for."""
    reg = DraftRegistry(session_id="s1")
    reg.declare(
        DraftEntry(
            kind="page",
            id="p1",
            name="contact",
            app_code="orangeab",
            doc={
                "id": "p1",
                "name": "contact",
                "appCode": "orangeab",
                "clientCode": "SYSTEM",
                "rootComponent": "root",
                "componentDefinition": {"root": {"key": "root", "type": "Grid"}},
            },
        )
    )
    reg.entries()[0].sent = dict(reg.entries()[0].doc)
    token = open_drafts.set(reg)
    yield reg
    open_drafts.reset(token)


@pytest.mark.asyncio
async def test_read_of_open_page_never_reaches_the_network(page_open):
    client = SaasClient("http://unreachable.invalid")
    result = await client.get("/api/ui/pages/p1")
    assert result.success
    assert result.data["name"] == "contact"


@pytest.mark.asyncio
async def test_read_returns_a_copy_so_an_abandoned_edit_cannot_leak(page_open):
    """A tool that mutates what it read and then fails must not corrupt the draft."""
    client = SaasClient("http://unreachable.invalid")
    got = (await client.get("/api/ui/pages/p1")).data
    got["componentDefinition"]["root"]["type"] = "Vandalised"
    assert page_open.entry("page", "p1").doc["componentDefinition"]["root"]["type"] == "Grid"


@pytest.mark.asyncio
async def test_update_of_open_page_is_held_and_patched(page_open):
    client = SaasClient("http://unreachable.invalid")
    page_open.stream = _RecordingStream()

    doc = (await client.get("/api/ui/pages/p1")).data
    doc["componentDefinition"]["btn"] = {"key": "btn", "type": "Button"}
    result = await client.put("/api/ui/pages/p1", json=doc)

    assert result.success
    entry = page_open.entry("page", "p1")
    assert entry.touched
    assert "btn" in entry.doc["componentDefinition"]

    (event,) = page_open.stream.patches
    assert event["kind"] == "page"
    # Only the new component travels, not the whole definition.
    assert set(event["patch"]["changed"]) == {"btn"}
    assert event["patch"]["removed"] == []


@pytest.mark.asyncio
async def test_override_save_of_open_page_is_held(page_open):
    """save_page strips the id and POSTs when the page belongs to another client.

    Appbuilder's pages are SYSTEM-owned, so this is the ordinary save path there.
    Treating it as a create would send every held edit straight to the database.
    """
    client = SaasClient("http://unreachable.invalid")
    page_open.stream = _RecordingStream()

    doc = (await client.get("/api/ui/pages/p1")).data
    doc.pop("id")
    doc["componentDefinition"]["btn"] = {"key": "btn", "type": "Button"}

    result = await client.post("/api/ui/pages", json=doc)

    assert result.success
    assert page_open.entry("page", "p1").touched
    assert len(page_open.stream.patches) == 1


@pytest.mark.asyncio
async def test_an_action_payload_carrying_a_name_is_not_mistaken_for_a_save(page_open):
    """POST /api/core/functions/execute with a `name` argument must still run."""
    client = SaasClient("http://unreachable.invalid")
    held = await client._serve_from_draft(
        "POST", "/api/core/functions/execute", {"name": "contact"}
    )
    assert held is None


@pytest.mark.asyncio
async def test_object_not_declared_open_is_not_held(page_open):
    """The theme case: not open, so it goes to the database like any other write."""
    client = SaasClient("http://unreachable.invalid")
    held = await client._serve_from_draft("PUT", "/api/ui/themes/t9", {"id": "t9"})
    assert held is None


@pytest.mark.asyncio
async def test_nothing_is_held_when_no_drafts_are_declared():
    """The plain `ai` chat page declares nothing and must behave exactly as before."""
    client = SaasClient("http://unreachable.invalid")
    assert await client._serve_from_draft("PUT", "/api/ui/pages/p1", {"id": "p1"}) is None


@pytest.mark.asyncio
async def test_delete_is_never_held(page_open):
    client = SaasClient("http://unreachable.invalid")
    assert await client._serve_from_draft("DELETE", "/api/ui/pages/p1", None) is None


# ── Patch shape ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_removals_and_page_fields_travel(page_open):
    client = SaasClient("http://unreachable.invalid")
    page_open.stream = _RecordingStream()

    doc = (await client.get("/api/ui/pages/p1")).data
    doc["componentDefinition"] = {}
    doc["permission"] = "Authorities.Logged_IN"
    await client.put("/api/ui/pages/p1", json=doc)

    patch = page_open.stream.patches[0]["patch"]
    assert patch["removed"] == ["root"]
    assert patch["fields"]["permission"] == "Authorities.Logged_IN"


@pytest.mark.asyncio
async def test_non_page_kinds_send_the_whole_document():
    reg = DraftRegistry(session_id="s2")
    reg.declare(
        DraftEntry(kind="storage", id="s1", name="Lead", app_code="orangeab",
                   doc={"id": "s1", "name": "Lead", "appCode": "orangeab"})
    )
    reg.stream = _RecordingStream()
    token = open_drafts.set(reg)
    try:
        client = SaasClient("http://unreachable.invalid")
        await client.put(
            "/api/core/storages/s1",
            json={"id": "s1", "name": "Lead", "appCode": "orangeab", "isAudited": True},
        )
    finally:
        open_drafts.reset(token)

    patch = reg.stream.patches[0]["patch"]
    assert patch["doc"]["isAudited"] is True


# ── The overlay: a page arrives as its difference from the saved version ─────


SAVED_PAGE = {
    "id": "p1",
    "name": "contact",
    "appCode": "orangeab",
    "clientCode": "SYSTEM",
    "version": 7,
    "rootComponent": "root",
    "componentDefinition": {
        "root": {"key": "root", "type": "Grid"},
        "old": {"key": "old", "type": "Text"},
        "kept": {"key": "kept", "type": "Button"},
    },
}


def _overlay_registry(overlay):
    reg = DraftRegistry(session_id="s3")
    reg.declare(
        DraftEntry(kind="page", id="p1", name="contact", app_code="orangeab",
                   overlay=overlay)
    )
    reg.stream = _RecordingStream()
    return reg


@pytest.mark.asyncio
async def test_overlay_is_laid_over_the_saved_page(monkeypatch):
    """The agent must see the user's unsaved work, not the saved version."""
    reg = _overlay_registry({
        "changed": {"kept": {"key": "kept", "type": "Button", "edited": True}},
        "removed": ["old"],
        "fields": {},
    })
    client = _client_serving(monkeypatch, SAVED_PAGE)

    token = open_drafts.set(reg)
    try:
        doc = (await client.get("/api/ui/pages/p1")).data
    finally:
        open_drafts.reset(token)

    comps = doc["componentDefinition"]
    assert comps["kept"]["edited"] is True, "the user's unsaved edit must be visible"
    assert "old" not in comps, "a component the user deleted must be gone"
    assert "root" in comps, "untouched components must survive from the saved page"


@pytest.mark.asyncio
async def test_a_clean_page_sends_an_empty_overlay_and_reads_as_saved(monkeypatch):
    reg = _overlay_registry({})
    client = _client_serving(monkeypatch, SAVED_PAGE)

    token = open_drafts.set(reg)
    try:
        doc = (await client.get("/api/ui/pages/p1")).data
    finally:
        open_drafts.reset(token)

    assert doc["componentDefinition"].keys() == SAVED_PAGE["componentDefinition"].keys()


@pytest.mark.asyncio
async def test_patch_is_measured_against_the_users_copy_not_the_saved_one(monkeypatch):
    """The user's own unsaved edit must not come back as if the agent made it."""
    reg = _overlay_registry({
        "changed": {"kept": {"key": "kept", "type": "Button", "edited": True}},
        "removed": [],
        "fields": {},
    })
    client = _client_serving(monkeypatch, SAVED_PAGE)

    token = open_drafts.set(reg)
    try:
        doc = (await client.get("/api/ui/pages/p1")).data
        doc["componentDefinition"]["new"] = {"key": "new", "type": "Button"}
        await client.put("/api/ui/pages/p1", json=doc)
    finally:
        open_drafts.reset(token)

    changed = reg.stream.patches[0]["patch"]["changed"]
    assert set(changed) == {"new"}


@pytest.mark.asyncio
async def test_saved_copy_unreadable_falls_back_to_the_overlay_alone(monkeypatch):
    """A 404 on the saved page must not lose the user's unsaved work."""
    reg = _overlay_registry({
        "changed": {"solo": {"key": "solo", "type": "Text"}},
        "removed": [],
        "fields": {},
    })
    client = _client_serving(monkeypatch, None, status=404)

    token = open_drafts.set(reg)
    try:
        doc = (await client.get("/api/ui/pages/p1")).data
    finally:
        open_drafts.reset(token)

    assert "solo" in doc["componentDefinition"]


def _client_serving(monkeypatch, payload, status: int = 200):
    """A SaasClient whose network always answers with `payload`."""
    client = SaasClient("http://unreachable.invalid")

    class _Response:
        status_code = status
        content = b"{}" if payload is not None else b""
        headers = {"content-type": "application/json"}
        text = ""

        @staticmethod
        def json():
            return payload

    class _Http:
        @staticmethod
        async def request(**_kwargs):
            return _Response()

    monkeypatch.setattr(client, "_get_client", lambda: _Http())
    return client


class _RecordingStream:
    """Stands in for AgentEventStream, capturing what the client would be told."""

    def __init__(self) -> None:
        self.patches: list[dict] = []
        self.changes: list[dict] = []

    async def emit_draft_patch(self, **kwargs) -> None:
        self.patches.append(kwargs)

    async def emit_object_changed(self, **kwargs) -> None:
        self.changes.append(kwargs)


# ── Declaring by API, and declaring without shipping a copy ──────────────────


@pytest.mark.asyncio
async def test_a_clean_declaration_is_filled_from_the_database(monkeypatch):
    """The workspace declares a clean tab without uploading a copy of it.

    Declaring is what makes the agent's writes wait for the user's Save; the
    document itself is byte-for-byte the saved one, so shipping it would be
    paying to tell us something we can already read.
    """
    reg = DraftRegistry(session_id="s4")
    reg.declare(DraftEntry(kind="storage", id="s1", name="Lead", app_code="orangeab"))
    reg.stream = _RecordingStream()
    saved = {"id": "s1", "name": "Lead", "appCode": "orangeab", "isAudited": False}
    client = _client_serving(monkeypatch, saved)

    token = open_drafts.set(reg)
    try:
        doc = (await client.get("/api/core/storages/s1")).data
        doc["isAudited"] = True
        await client.put("/api/core/storages/s1", json=doc)
    finally:
        open_drafts.reset(token)

    assert doc["name"] == "Lead", "the declaration must be filled from the saved copy"
    assert reg.entry("storage", "s1").doc["isAudited"] is True
    assert reg.stream.patches[0]["patch"]["doc"]["isAudited"] is True


def test_an_api_path_resolves_to_the_same_kind_the_intercept_matches():
    """The workspace names the API; the intercept matches on kind. One table."""
    for api, kind in [
        ("/api/core/storages", "storage"),
        ("/api/ui/themes", "theme"),
        ("/api/core/eventActions", "eventaction"),
        ("/api/ui/pages", "page"),
    ]:
        assert DraftRegistry.resolve(api) == (kind, None, None)


# ── Partial writes: the leak that shipped, and the fail-closed rule ──────────
#
# The resolver used to return nothing for a path with an extra segment, so
# PATCH /api/ui/pages/{id}/components/{key} — patch_component_props, the most
# used editing tool in the service — went straight to the database while the
# agent told the user the change was waiting for their Save. These pin both the
# specific fix and the general rule that anything unrecognised refuses instead.


def test_a_partial_write_path_is_recognised_not_ignored():
    assert DraftRegistry.resolve("/api/ui/pages/p1/components/btn") == (
        "page", "p1", "components/btn",
    )
    assert DraftRegistry.resolve("/api/ui/pages/p1/events/onLoad") == (
        "page", "p1", "events/onLoad",
    )


@pytest.mark.asyncio
async def test_component_patch_on_an_open_page_is_held(page_open):
    client = SaasClient("http://unreachable.invalid")
    page_open.stream = _RecordingStream()

    result = await client.patch(
        "/api/ui/pages/p1/components/root",
        json={
            "componentData": {"key": "root", "type": "Grid", "name": "Renamed"},
            "expectedComponentVersion": 1,
            "message": "x",
        },
    )

    assert result.success
    entry = page_open.entry("page", "p1")
    assert entry.doc["componentDefinition"]["root"]["name"] == "Renamed"
    assert set(page_open.stream.patches[0]["patch"]["changed"]) == {"root"}


@pytest.mark.asyncio
async def test_event_put_on_an_open_page_is_held(page_open):
    client = SaasClient("http://unreachable.invalid")
    page_open.stream = _RecordingStream()

    result = await client.put(
        "/api/ui/pages/p1/events/onLoad",
        json={"definition": {"steps": {"a": {}}}, "expectedEventVersion": 1, "message": "x"},
    )

    assert result.success
    assert page_open.entry("page", "p1").doc["eventFunctions"]["onLoad"] == {"steps": {"a": {}}}


@pytest.mark.asyncio
async def test_an_unhandled_partial_write_refuses_rather_than_leaking(page_open):
    """Fail closed. Saving behind the user's back is the one unacceptable outcome."""
    client = SaasClient("http://unreachable.invalid")
    result = await client.patch("/api/ui/pages/p1/translations/en", json={"a": 1})
    assert not result.success
    assert "open and unsaved" in result.error


@pytest.mark.asyncio
async def test_a_partial_write_to_an_object_nobody_declared_still_goes_through(page_open):
    client = SaasClient("http://unreachable.invalid")
    held = await client._serve_from_draft(
        "PATCH", "/api/ui/pages/OTHER/components/x", {"componentData": {}},
    )
    assert held is None


# ── Which surface a real write landed on ─────────────────────────────────────
#
# A write the user does not have open still reaches the database, and the client
# is told so it can refresh whatever shows the object. With the draft surface in
# play "reached the database" is no longer one place: the editor reads and saves
# the draft, so an announcement that does not say which surface leaves it
# refetching the live copy and finding none of the change it was just told about.
# The verb and the path are identical either way, so nothing downstream can infer
# it.


@pytest.mark.asyncio
async def test_a_drafted_write_announces_the_surface_it_went_to(monkeypatch):
    reg = DraftRegistry(session_id="s-draft")
    reg.stream = _RecordingStream()
    client = _client_serving(monkeypatch, {"id": "p9", "name": "home", "appCode": "orangeab"})

    token = open_drafts.set(reg)
    try:
        await client.put(
            "/api/ui/pages/p9",
            json={"id": "p9", "name": "home", "appCode": "orangeab"},
            params={"draft": "true"},
        )
    finally:
        open_drafts.reset(token)

    assert len(reg.stream.changes) == 1
    change = reg.stream.changes[0]
    assert change["kind"] == "page"
    assert change["obj_id"] == "p9"
    assert change["draft"] is True


@pytest.mark.asyncio
async def test_a_live_write_says_so_rather_than_leaving_it_unset(monkeypatch):
    reg = DraftRegistry(session_id="s-live")
    reg.stream = _RecordingStream()
    client = _client_serving(monkeypatch, {"id": "t1", "name": "Base", "appCode": "orangeab"})

    token = open_drafts.set(reg)
    try:
        await client.put("/api/ui/themes/t1", json={"id": "t1", "name": "Base"})
    finally:
        open_drafts.reset(token)

    assert reg.stream.changes[0]["draft"] is False
