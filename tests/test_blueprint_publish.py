"""Putting what was built on the site.

The step that was missing entirely. The build creates unpublished and the
builder authors onto a draft — both correct — with a consequence nobody priced
in: a page could be built completely and still answer 404 on the live URL AND on
the draft host, so View site and View draft were both blank for exactly the
pages just made.

Two failures are pinned here because both actually happened:

  A partial publish reported as a success. `publishAll` attempts every pending
  draft and reports per object, so `attempted=2, published=0` is a real answer
  with the reason sitting in `results`. Reading the count and dropping the rest
  turned a self-inflicted version conflict into "2 published".

  Publishing pages without their storages, which leaves a live form posting
  into a schema nobody has published — broken at the one moment it matters.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.blueprint import publish


class _Result:
    def __init__(self, data, success=True, error=""):
        self.data, self.success, self.error = data, success, error


class _FakeClient:
    """Stands in for the platform, recording the order calls arrive in."""

    def __init__(self, pending_by_service=None, publish_by_service=None, fail=()):
        self.pending_by_service = pending_by_service or {}
        self.publish_by_service = publish_by_service or {}
        self.fail = set(fail)
        self.calls: list[str] = []

    def _service(self, url: str) -> str:
        return "core" if "/api/core/" in url else "ui"

    async def get(self, url, headers=None, params=None):
        service = self._service(url)
        self.calls.append(f"GET {service}")
        if service in self.fail:
            return _Result(None, success=False, error="unreachable")
        return _Result(self.pending_by_service.get(service, {}))

    async def post(self, url, headers=None, params=None):
        service = self._service(url)
        self.calls.append(f"POST {service}")
        if service in self.fail:
            return _Result(None, success=False, error="boom")
        return _Result(self.publish_by_service.get(service, {}))


def _use(monkeypatch, client):
    monkeypatch.setattr(publish, "get_saas_client", lambda: client)
    # The plan link is build_job's job and has its own tests; here it must
    # simply not fail a publish.
    async def no_link(app_code, headers, client_code, names):
        return {}
    monkeypatch.setattr(publish, "_link_plans", no_link)
    return client


PENDING = {
    "ui": {"PAGE": [{"name": "blogList", "message": "Header title reads Notes"}],
           "APPLICATION": [{"name": "crumbco"}]},
    "core": {"STORAGE": [{"name": "orderRequest"}]},
}


def test_pending_counts_both_services_and_says_what_each_thing_is(monkeypatch):
    _use(monkeypatch, _FakeClient(pending_by_service=PENDING))
    result = asyncio.run(publish.pending("crumbco", {}))
    assert result["count"] == 3
    # The platform's vocabulary is PAGE/STORAGE/APPLICATION. Nobody looking at
    # their own site calls it an APPLICATION.
    whats = {i["name"]: i["what"] for i in result["items"]}
    assert whats == {
        "blogList": "page", "crumbco": "the site's settings", "orderRequest": "store",
    }


def test_a_surface_that_cannot_be_read_is_reported_not_counted_as_empty(monkeypatch):
    # "Nothing to publish" and "we could not find out" are different, and only
    # one of them means the button should go quiet.
    _use(monkeypatch, _FakeClient(pending_by_service=PENDING, fail={"core"}))
    result = asyncio.run(publish.pending("crumbco", {}))
    assert result["unreachable"] == ["core"]
    assert result["count"] == 2


def test_storages_are_published_before_pages(monkeypatch):
    """A page that goes live against an unpublished storage is broken until the
    storage catches up, and nothing on screen would say why."""
    client = _use(monkeypatch, _FakeClient(pending_by_service=PENDING))
    asyncio.run(publish.publish_all("crumbco", {}))
    posts = [c for c in client.calls if c.startswith("POST")]
    assert posts == ["POST core", "POST ui"]


def test_a_publish_that_published_nothing_does_not_report_success(monkeypatch):
    """The failure that hid a self-inflicted version conflict for an hour.

    `publishAll` answers 200 with `published: 0` and the reason per object. The
    first version of this read the count and dropped `results`, so a run that
    refused both pages was reported as "2 published".
    """
    refused = {
        "attempted": 2, "published": 0,
        "results": [
            {"name": "blogList", "published": False,
             "error": "Please reload to get the new version before making changes"},
            {"name": "blogPost", "published": False, "error": "same"},
        ],
    }
    _use(monkeypatch, _FakeClient(
        pending_by_service=PENDING, publish_by_service={"ui": refused},
    ))
    result = asyncio.run(publish.publish_all("crumbco", {}))
    line = next(p for p in result["published"] if p.startswith("ui:"))
    assert "0 of 2 published" in line
    # Named, not counted. "2 were refused" sends somebody looking; the reason
    # was already in the answer.
    assert "blogList" in line
    assert "Please reload" in line


def test_one_service_failing_does_not_stop_the_other(monkeypatch):
    # Stopping at the first failure leaves the site half published with no
    # record of which half — the state hardest to reason about afterwards.
    client = _use(monkeypatch, _FakeClient(
        pending_by_service=PENDING,
        publish_by_service={"ui": {"attempted": 1, "published": 1}},
        fail=set(),
    ))
    client.fail = {"core"}
    result = asyncio.run(publish.publish_all("crumbco", {}))
    assert "core" in result["failed"]
    assert any(p.startswith("ui:") for p in result["published"])


def test_everything_failing_is_an_error_not_a_quiet_success(monkeypatch):
    _use(monkeypatch, _FakeClient(pending_by_service=PENDING, fail={"core", "ui"}))
    with pytest.raises(publish.PublishError) as caught:
        asyncio.run(publish.publish_all("crumbco", {}))
    assert "Nothing could be published" in caught.value.message


def test_what_remains_is_asked_again_rather_than_assumed(monkeypatch):
    # A publish that silently skipped something must not leave the board
    # claiming the site is up to date.
    client = _use(monkeypatch, _FakeClient(
        pending_by_service=PENDING,
        publish_by_service={"ui": {"attempted": 2, "published": 0}},
    ))
    result = asyncio.run(publish.publish_all("crumbco", {}))
    assert result["remaining"] == 3
    # Asked before AND after: once to know whose plan to link, once to report.
    assert client.calls.count("GET ui") >= 2
