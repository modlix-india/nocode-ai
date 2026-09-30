"""Which component catalog wins, and why a locally-regenerated one has to.

The CDN copy is republished only by nocode-ui's CI. A developer who edits
component properties and regenerates locally would otherwise keep validating
against a catalog that predates their change, and watch the agent reject the
property they just added as unknown. Comparing `generatedAt` fixes that with no
flag to remember.

Ported from the same fix in modlix-mcp's `catalog.py` (2026-08-26).
"""

from __future__ import annotations

import json

import pytest

from app.agents.appbuilder.catalog import ComponentCatalog, _resolve_local_catalog


def _catalog(stamp: str, components: dict | None = None) -> dict:
    return {"generatedAt": stamp, "components": components or {"Grid": {"properties": {}}}}


def _write(tmp_path, stamp: str, components: dict | None = None):
    path = tmp_path / "component-catalog.json"
    path.write_text(json.dumps(_catalog(stamp, components)), encoding="utf-8")
    return path


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, payload: dict | None, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, _url):
        if self._error:
            raise self._error
        return _FakeResponse(self._payload)


@pytest.fixture
def cdn(monkeypatch):
    """Stub httpx so the loader sees whatever CDN payload a test wants."""
    def _install(payload: dict | None, error: Exception | None = None):
        import app.agents.appbuilder.catalog as mod
        monkeypatch.setattr(
            mod.httpx, "AsyncClient", lambda **kw: _FakeClient(payload, error),
        )
    return _install


# ── generatedAt precedence ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_newer_local_catalog_beats_the_cdn(tmp_path, cdn):
    cdn(_catalog("2026-01-01T00:00:00Z", {"Grid": {}}))
    _write(tmp_path, "2026-08-27T00:00:00Z", {"Grid": {}, "TextBox": {}})

    cat = ComponentCatalog("https://cdn.example/component-catalog.json", str(tmp_path))
    assert await cat.load() is True
    assert set(cat.get_all_types()) == {"Grid", "TextBox"}


@pytest.mark.asyncio
async def test_older_local_catalog_leaves_the_cdn_alone(tmp_path, cdn):
    cdn(_catalog("2026-08-27T00:00:00Z", {"Grid": {}, "TextBox": {}}))
    _write(tmp_path, "2026-01-01T00:00:00Z", {"Grid": {}})

    cat = ComponentCatalog("https://cdn.example/component-catalog.json", str(tmp_path))
    assert await cat.load() is True
    assert set(cat.get_all_types()) == {"Grid", "TextBox"}


@pytest.mark.asyncio
async def test_equal_stamps_leave_the_cdn_alone(tmp_path, cdn):
    cdn(_catalog("2026-08-27T00:00:00Z", {"Grid": {}, "TextBox": {}}))
    _write(tmp_path, "2026-08-27T00:00:00Z", {"Grid": {}})

    cat = ComponentCatalog("https://cdn.example/component-catalog.json", str(tmp_path))
    await cat.load()
    assert set(cat.get_all_types()) == {"Grid", "TextBox"}


@pytest.mark.asyncio
async def test_an_unstamped_cdn_catalog_is_never_overridden(tmp_path, cdn):
    """Cannot compare, so do not guess."""
    cdn({"components": {"Grid": {}, "TextBox": {}}})
    _write(tmp_path, "2026-08-27T00:00:00Z", {"Grid": {}})

    cat = ComponentCatalog("https://cdn.example/component-catalog.json", str(tmp_path))
    await cat.load()
    assert set(cat.get_all_types()) == {"Grid", "TextBox"}


@pytest.mark.asyncio
async def test_an_unstamped_local_catalog_is_not_preferred(tmp_path, cdn):
    cdn(_catalog("2026-01-01T00:00:00Z", {"Grid": {}, "TextBox": {}}))
    (tmp_path / "component-catalog.json").write_text(
        json.dumps({"components": {"Grid": {}}}), encoding="utf-8",
    )
    cat = ComponentCatalog("https://cdn.example/component-catalog.json", str(tmp_path))
    await cat.load()
    assert set(cat.get_all_types()) == {"Grid", "TextBox"}


@pytest.mark.asyncio
async def test_corrupt_local_catalog_does_not_break_the_cdn_load(tmp_path, cdn):
    cdn(_catalog("2026-01-01T00:00:00Z", {"Grid": {}, "TextBox": {}}))
    (tmp_path / "component-catalog.json").write_text("{ not json", encoding="utf-8")

    cat = ComponentCatalog("https://cdn.example/component-catalog.json", str(tmp_path))
    assert await cat.load() is True
    assert set(cat.get_all_types()) == {"Grid", "TextBox"}


# ── Fallbacks ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_dead_cdn_falls_back_to_the_local_catalog(tmp_path, cdn):
    cdn(None, error=RuntimeError("connection refused"))
    _write(tmp_path, "2026-08-27T00:00:00Z", {"Grid": {}, "TextBox": {}})

    cat = ComponentCatalog("https://cdn.example/component-catalog.json", str(tmp_path))
    assert await cat.load() is True
    assert set(cat.get_all_types()) == {"Grid", "TextBox"}


@pytest.mark.asyncio
async def test_a_dead_cdn_with_no_local_catalog_uses_the_fallback(tmp_path, cdn):
    cdn(None, error=RuntimeError("connection refused"))
    cat = ComponentCatalog("https://cdn.example/component-catalog.json", str(tmp_path))
    assert await cat.load() is False
    assert cat.get_all_types(), "the bundled fallback must still yield components"


@pytest.mark.asyncio
async def test_an_explicit_file_url_is_used_as_given(tmp_path, cdn):
    path = _write(tmp_path, "2026-01-01T00:00:00Z", {"OnlyThis": {}})
    cat = ComponentCatalog(f"file://{path}")
    assert await cat.load() is True
    assert cat.get_all_types() == ["OnlyThis"]


@pytest.mark.asyncio
async def test_no_url_configured_still_picks_up_a_local_build(tmp_path):
    _write(tmp_path, "2026-08-27T00:00:00Z", {"Grid": {}, "TextBox": {}})
    cat = ComponentCatalog("", str(tmp_path))
    assert await cat.load() is True
    assert set(cat.get_all_types()) == {"Grid", "TextBox"}


# ── Path resolution ──────────────────────────────────────────────────────


def test_resolve_accepts_the_file_the_dist_dir_and_the_client_dir(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    path = dist / "component-catalog.json"
    path.write_text("{}", encoding="utf-8")

    assert _resolve_local_catalog(str(path)) == path       # the file itself
    assert _resolve_local_catalog(str(dist)) == path       # its dist/ dir
    assert _resolve_local_catalog(str(tmp_path)) == path   # the client dir


def test_resolve_returns_none_for_a_path_with_no_catalog(tmp_path):
    assert _resolve_local_catalog(str(tmp_path / "nowhere")) is None
