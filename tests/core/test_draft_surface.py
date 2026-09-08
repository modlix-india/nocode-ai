"""The draft surface, and the reason it is never assumed.

`?draft=true` is an ordinary query parameter. A deployment that predates the
draft work does not reject it and does not honour it: Spring drops unknown
parameters and performs a normal LIVE update. Verified the hard way against a
running local `ui` service, where a "draft" write bumped the live version and
published the change.

So every one of these tests is really about the same thing: the agent must not
tell someone their work is waiting for review when it has gone live.
"""

from __future__ import annotations

import pytest

from app.agents.appbuilder.tools.modlix import _draft_surface as ds
from app.core.tools.base import ToolResult


@pytest.fixture(autouse=True)
def _clean_support_cache():
    ds.reset_support_cache()
    token = ds.draft_mode.set(False)
    yield
    ds.draft_mode.reset(token)
    ds.reset_support_cache()


class _Client:
    """A SaasClient stand-in that answers with whatever the test wants."""

    def __init__(self, get=None, post=None):
        self._get, self._post = get, post
        self.calls: list[tuple[str, str, dict | None]] = []

    async def get(self, path, headers=None, params=None):
        self.calls.append(("GET", path, params))
        return self._get if self._get is not None else ToolResult(success=False, error="404")

    async def post(self, path, headers=None, json=None, params=None):
        self.calls.append(("POST", path, params))
        return self._post if self._post is not None else ToolResult(success=False, error="boom")


# ── The probe ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_json_from_the_publish_route_means_supported():
    c = _Client(get=ToolResult(success=True, data={"page": []}))
    assert await ds.supported(c, {}, "app1") is True


@pytest.mark.asyncio
async def test_html_with_a_200_does_NOT_mean_supported():
    """The exact trap: a stale gateway falls through to the SPA and returns 200
    with an HTML body. A status check reads that as support and every later
    write silently goes live."""
    c = _Client(get=ToolResult(success=True, data="<!DOCTYPE html><html>App Builder</html>"))
    assert await ds.supported(c, {}, "app1") is False


@pytest.mark.asyncio
async def test_a_failed_probe_means_not_supported():
    c = _Client(get=ToolResult(success=False, error="HTTP 404"))
    assert await ds.supported(c, {}, "app1") is False


@pytest.mark.asyncio
async def test_the_probe_runs_once_per_app():
    c = _Client(get=ToolResult(success=True, data={}))
    await ds.supported(c, {}, "app1")
    await ds.supported(c, {}, "app1")
    assert len(c.calls) == 1


@pytest.mark.asyncio
async def test_wanting_drafts_is_not_enough_to_get_them():
    ds.draft_mode.set(True)
    c = _Client(get=ToolResult(success=True, data="<html>"))
    assert ds.wanted() is True
    assert await ds.active(c, {}, "app1") is False


@pytest.mark.asyncio
async def test_support_alone_is_not_enough_either():
    c = _Client(get=ToolResult(success=True, data={}))
    assert await ds.active(c, {}, "app1") is False  # draft_mode is off


# ── The flag on the wire ─────────────────────────────────────────────────────


def test_the_flag_is_added_only_when_on():
    assert ds.params_with_draft(None, False) is None
    assert ds.params_with_draft({"a": 1}, False) == {"a": 1}
    assert ds.params_with_draft(None, True) == {"draft": "true"}
    assert ds.params_with_draft({"a": 1}, True) == {"a": 1, "draft": "true"}


def test_adding_the_flag_does_not_mutate_the_caller_s_params():
    original = {"a": 1}
    ds.params_with_draft(original, True)
    assert original == {"a": 1}


# ── The draft hostname ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_existing_link_is_returned_and_not_rotated():
    """Minting rotates, which revokes a link the user may have shared. Reading
    first is the whole reason ensure_ exists."""
    c = _Client(get=ToolResult(success=True, data={"urlPattern": "abc123.dev.modlix.com"}))
    url, err = await ds.ensure_draft_url(c, {}, "app1")
    assert (url, err) == ("https://abc123.dev.modlix.com", None)
    assert [m for m, _, _ in c.calls] == ["GET"]


@pytest.mark.asyncio
async def test_a_missing_link_is_minted():
    c = _Client(
        get=ToolResult(success=False, error="HTTP 404: not found"),
        post=ToolResult(success=True, data={"urlPattern": "new999.dev.modlix.com"}),
    )
    url, err = await ds.ensure_draft_url(c, {}, "app1")
    assert (url, err) == ("https://new999.dev.modlix.com", None)
    assert [m for m, _, _ in c.calls] == ["GET", "POST"]


@pytest.mark.asyncio
async def test_a_real_read_error_is_not_mistaken_for_a_missing_link():
    """A 403 must not trigger a mint that rotates someone's working link."""
    c = _Client(get=ToolResult(success=False, error="HTTP 403: forbidden"))
    url, err = await ds.ensure_draft_url(c, {}, "app1")
    assert url is None and "403" in err
    assert [m for m, _, _ in c.calls] == ["GET"]


def test_a_bare_hostname_becomes_a_url():
    assert ds._host_of({"urlPattern": "x.dev.modlix.com"}) == "https://x.dev.modlix.com"
    assert ds._host_of({"urlPattern": "https://x.dev.modlix.com"}) == "https://x.dev.modlix.com"
    assert ds._host_of({}) is None
    assert ds._host_of(None) is None
