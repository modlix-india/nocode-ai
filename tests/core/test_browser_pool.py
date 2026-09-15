"""The shared browser pool: one Chromium per worker, handed out as contexts.

Why this exists (dev, 2026-09-15): every Playwright call site launched its own
browser, so eighteen were live at once, twelve of them idle for three hours.
Measured per browser: ~379 MB of fixed overhead (driver node, browser main,
gpu, network utility, zygotes) plus ~216 MB for the renderer that actually
draws the page. Chromium never shares renderers across contexts, so the 216 MB
is a floor; the 379 MB was pure repetition and is what this module removes.

These tests use fakes. `scratchpad/pool_check.py` covers the same properties
against a real Chromium by counting processes.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.services import browser_pool as bp


class _FakeContext:
    def __init__(self, browser):
        self.browser = browser
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self, serial):
        self.serial = serial
        self.connected = True
        self.contexts = []

    def is_connected(self):
        return self.connected

    async def new_context(self, **kwargs):
        ctx = _FakeContext(self)
        ctx.kwargs = kwargs
        self.contexts.append(ctx)
        return ctx

    async def close(self):
        self.connected = False


class _FakeChromium:
    def __init__(self):
        self.launches = 0

    async def launch(self, **kwargs):
        self.launches += 1
        return _FakeBrowser(self.launches)


class _FakePlaywright:
    def __init__(self):
        self.chromium = _FakeChromium()
        self.stopped = False

    async def stop(self):
        self.stopped = True


@pytest.fixture(autouse=True)
def _reset_pool(monkeypatch):
    fake = _FakePlaywright()

    async def _fake_get_playwright():
        bp._playwright = fake
        return fake

    monkeypatch.setattr(bp, "_get_playwright", _fake_get_playwright)
    bp._playwright = None
    bp._browsers.clear()
    bp._browser_last_used.clear()
    bp._open_contexts.clear()
    bp._loop = None
    bp._stat_launches = bp._stat_relaunches = bp._stat_contexts = 0
    yield fake
    bp._browsers.clear()
    bp._open_contexts.clear()
    bp._playwright = None
    bp._loop = None


@pytest.mark.asyncio
async def test_one_browser_serves_many_contexts(_reset_pool):
    ctxs = [await bp.open_context(bp.INTERNAL) for _ in range(4)]
    assert _reset_pool.chromium.launches == 1, "each context must NOT launch a browser"
    assert len({id(c.browser) for c in ctxs}) == 1
    assert bp.stats()["open_contexts"] == 4
    for c in ctxs:
        await bp.close_context(c)
    assert bp.stats()["open_contexts"] == 0
    assert all(c.closed for c in ctxs)


@pytest.mark.asyncio
async def test_closing_contexts_leaves_the_browser_up(_reset_pool):
    ctx = await bp.open_context(bp.INTERNAL)
    await bp.close_context(ctx)
    assert bp._browsers[bp.INTERNAL].is_connected() is True
    await bp.open_context(bp.INTERNAL)
    assert _reset_pool.chromium.launches == 1, "reused, not relaunched"


@pytest.mark.asyncio
async def test_internal_and_external_never_share_a_browser(_reset_pool):
    """Untrusted third-party pages are rendered unsandboxed (Playwright's
    `chromiumSandbox` default), so they stay out of the process tree that
    renders Modlix pages holding real end-user tokens."""
    a = await bp.open_context(bp.INTERNAL)
    b = await bp.open_context(bp.EXTERNAL)
    assert a.browser is not b.browser
    assert _reset_pool.chromium.launches == 2


@pytest.mark.asyncio
async def test_context_kwargs_reach_the_browser(_reset_pool):
    ctx = await bp.open_context(bp.INTERNAL, viewport={"width": 800, "height": 600})
    assert ctx.kwargs["viewport"] == {"width": 800, "height": 600}


@pytest.mark.asyncio
async def test_a_waiter_gets_the_next_freed_permit(_reset_pool, monkeypatch):
    """Past the cap, callers QUEUE. Erroring immediately would turn a burst of
    parallel tool calls into failures; spawning past the cap is what filled the
    box with Chromium in the first place."""
    monkeypatch.setattr(settings, "BROWSER_MAX_CONTEXTS", 2)
    bp._loop = None  # force the semaphore to be rebuilt at the new size

    held = [await bp.open_context(bp.INTERNAL) for _ in range(2)]
    waiter = asyncio.create_task(bp.open_context(bp.INTERNAL))
    await asyncio.sleep(0)
    assert not waiter.done(), "third caller must wait, not be granted"

    await bp.close_context(held[0])
    third = await asyncio.wait_for(waiter, timeout=1)
    assert third is not None
    assert bp.stats()["open_contexts"] == 2


@pytest.mark.asyncio
async def test_waiting_past_the_timeout_fails_cleanly(_reset_pool, monkeypatch):
    monkeypatch.setattr(settings, "BROWSER_MAX_CONTEXTS", 1)
    monkeypatch.setattr(settings, "BROWSER_ACQUIRE_TIMEOUT_SECONDS", 0.05)
    bp._loop = None

    await bp.open_context(bp.INTERNAL)
    with pytest.raises(bp.BrowserUnavailable):
        await bp.open_context(bp.INTERNAL)


@pytest.mark.asyncio
async def test_a_failed_context_returns_its_permit(_reset_pool, monkeypatch):
    """Otherwise the cap ratchets downward until the worker restarts."""
    monkeypatch.setattr(settings, "BROWSER_MAX_CONTEXTS", 1)
    bp._loop = None

    browser = await bp.get_browser(bp.INTERNAL)

    async def _boom(**kwargs):
        raise RuntimeError("out of memory")

    monkeypatch.setattr(browser, "new_context", _boom)
    with pytest.raises(RuntimeError):
        await bp.open_context(bp.INTERNAL)

    monkeypatch.undo()
    bp._loop = None
    ctx = await asyncio.wait_for(bp.open_context(bp.INTERNAL), timeout=1)
    assert ctx is not None


@pytest.mark.asyncio
async def test_persistent_contexts_cannot_take_the_whole_pool(_reset_pool, monkeypatch):
    """The reserve. Without it, enough conversations holding drive_page tabs take
    every permit and one-shot screenshots block until timeout and then fail."""
    monkeypatch.setattr(settings, "BROWSER_MAX_CONTEXTS", 5)
    monkeypatch.setattr(settings, "BROWSER_MAX_SESSIONS", 3)
    bp._loop = None

    held = [await bp.open_context(bp.INTERNAL, persistent=True) for _ in range(3)]
    assert bp.stats()["persistent_contexts"] == 3

    with pytest.raises(bp.BrowserUnavailable) as e:
        await bp.open_context(bp.INTERNAL, persistent=True, timeout=0.05)
    assert "session slot" in str(e.value)

    # The two reserved permits are still there for one-shot renders.
    a = await asyncio.wait_for(bp.open_context(bp.INTERNAL), timeout=1)
    b = await asyncio.wait_for(bp.open_context(bp.INTERNAL), timeout=1)
    assert bp.stats()["open_contexts"] == 5

    for c in [*held, a, b]:
        await bp.close_context(c)
    assert bp.stats()["open_contexts"] == 0
    assert bp.stats()["persistent_contexts"] == 0


@pytest.mark.asyncio
async def test_closing_a_persistent_context_frees_its_session_slot(_reset_pool, monkeypatch):
    monkeypatch.setattr(settings, "BROWSER_MAX_CONTEXTS", 4)
    monkeypatch.setattr(settings, "BROWSER_MAX_SESSIONS", 2)
    bp._loop = None

    one = await bp.open_context(bp.INTERNAL, persistent=True)
    two = await bp.open_context(bp.INTERNAL, persistent=True)
    with pytest.raises(bp.BrowserUnavailable):
        await bp.open_context(bp.INTERNAL, persistent=True, timeout=0.05)

    await bp.close_context(one)
    three = await asyncio.wait_for(
        bp.open_context(bp.INTERNAL, persistent=True, timeout=1), timeout=2)
    assert three is not None
    await bp.close_context(two)
    await bp.close_context(three)


@pytest.mark.asyncio
async def test_a_failed_persistent_context_frees_both_permits(_reset_pool, monkeypatch):
    monkeypatch.setattr(settings, "BROWSER_MAX_CONTEXTS", 2)
    monkeypatch.setattr(settings, "BROWSER_MAX_SESSIONS", 1)
    bp._loop = None

    browser = await bp.get_browser(bp.INTERNAL)

    async def _boom(**kwargs):
        raise RuntimeError("chromium said no")

    monkeypatch.setattr(browser, "new_context", _boom)
    with pytest.raises(RuntimeError):
        await bp.open_context(bp.INTERNAL, persistent=True)

    monkeypatch.undo()
    bp._loop = None
    ctx = await asyncio.wait_for(
        bp.open_context(bp.INTERNAL, persistent=True, timeout=1), timeout=2)
    assert ctx is not None


@pytest.mark.asyncio
async def test_session_cap_is_clamped_below_the_total(_reset_pool, monkeypatch):
    """A misconfiguration must not let sessions own every permit."""
    monkeypatch.setattr(settings, "BROWSER_MAX_CONTEXTS", 3)
    monkeypatch.setattr(settings, "BROWSER_MAX_SESSIONS", 99)  # nonsense on purpose
    bp._loop = None

    held = [await bp.open_context(bp.INTERNAL, persistent=True) for _ in range(2)]
    with pytest.raises(bp.BrowserUnavailable):
        await bp.open_context(bp.INTERNAL, persistent=True, timeout=0.05)
    assert bp.stats()["persistent_contexts"] == 2, "clamped to total - 1"
    for c in held:
        await bp.close_context(c)


@pytest.mark.asyncio
async def test_close_context_is_safe_twice(_reset_pool):
    ctx = await bp.open_context(bp.INTERNAL)
    await bp.close_context(ctx)
    await bp.close_context(ctx)  # must not double-release the permit
    assert bp.stats()["open_contexts"] == 0
    await bp.close_context(None)


@pytest.mark.asyncio
async def test_a_dead_browser_is_relaunched(_reset_pool):
    """Chromium can be killed out from under us: a cgroup OOM, or a crashed
    page taking the tree with it. `is_connected()` is the only honest check."""
    first = await bp.get_browser(bp.INTERNAL)
    first.connected = False
    second = await bp.get_browser(bp.INTERNAL)
    assert second is not first
    assert second.is_connected()
    assert bp.stats()["total_relaunches"] == 1


@pytest.mark.asyncio
async def test_concurrent_callers_launch_only_one_browser(_reset_pool):
    browsers = await asyncio.gather(*(bp.get_browser(bp.INTERNAL) for _ in range(8)))
    assert _reset_pool.chromium.launches == 1
    assert len({id(b) for b in browsers}) == 1


def _go_idle(*profiles, seconds=9999):
    """Backdate a browser's last-used stamp instead of sleeping."""
    import time
    for p in profiles:
        bp._browser_last_used[p] = time.monotonic() - seconds


@pytest.mark.asyncio
async def test_sweep_closes_an_idle_browser_but_never_a_busy_one(_reset_pool, monkeypatch):
    monkeypatch.setattr(settings, "BROWSER_IDLE_TTL_SECONDS", 60)
    busy = await bp.open_context(bp.INTERNAL)
    await bp.get_browser(bp.EXTERNAL)  # launched, no contexts
    _go_idle(bp.INTERNAL, bp.EXTERNAL)

    await bp._sweep_once()
    assert bp.EXTERNAL not in bp._browsers, "idle browser should be closed"
    assert bp.INTERNAL in bp._browsers, "a browser with a live context must stay"

    await bp.close_context(busy)
    _go_idle(bp.INTERNAL)
    await bp._sweep_once()
    assert bp.INTERNAL not in bp._browsers


@pytest.mark.asyncio
async def test_a_zero_ttl_setting_disables_idle_closing(_reset_pool, monkeypatch):
    monkeypatch.setattr(settings, "BROWSER_IDLE_TTL_SECONDS", 0)
    await bp.get_browser(bp.INTERNAL)
    _go_idle(bp.INTERNAL)
    await bp._sweep_once()
    assert bp.INTERNAL in bp._browsers


@pytest.mark.asyncio
async def test_sweep_hooks_run_before_the_idle_check(_reset_pool, monkeypatch):
    """Session reapers release contexts, and that is what decides whether a
    browser counts as idle. Running them afterwards would never free anything.

    Note the browser survives the pass that frees its LAST context: releasing a
    context restarts the idle clock, by design, so the close lands on the next
    sweep (in prod, TTL + up to one sweep interval).
    """
    order = []
    ctx = await bp.open_context(bp.INTERNAL)

    async def _hook():
        order.append("hook")
        await bp.close_context(ctx)

    monkeypatch.setattr(bp, "_sweep_hooks", [_hook])
    monkeypatch.setattr(settings, "BROWSER_IDLE_TTL_SECONDS", 60)
    _go_idle(bp.INTERNAL)

    await bp._sweep_once()
    assert order == ["hook"]
    assert ctx.closed is True, "the hook's context must be released by the sweep"
    assert bp.INTERNAL in bp._browsers, "idle clock restarts when the context closes"

    _go_idle(bp.INTERNAL)
    await bp._sweep_once()
    assert bp.INTERNAL not in bp._browsers


@pytest.mark.asyncio
async def test_a_failing_hook_does_not_stop_the_sweep(_reset_pool, monkeypatch):
    async def _bad():
        raise RuntimeError("reaper exploded")

    monkeypatch.setattr(bp, "_sweep_hooks", [_bad])
    monkeypatch.setattr(settings, "BROWSER_IDLE_TTL_SECONDS", 60)
    await bp.get_browser(bp.INTERNAL)
    _go_idle(bp.INTERNAL)
    await bp._sweep_once()
    assert bp.INTERNAL not in bp._browsers


@pytest.mark.asyncio
async def test_close_all_stops_everything_including_the_driver(_reset_pool):
    await bp.open_context(bp.INTERNAL)
    await bp.open_context(bp.EXTERNAL)
    assert await bp.close_all() == 2
    assert bp._browsers == {}
    assert bp._open_contexts == {}
    assert _reset_pool.stopped is True, "the node driver process must be stopped too"


@pytest.mark.asyncio
async def test_browser_context_releases_on_an_exception(_reset_pool):
    with pytest.raises(ValueError):
        async with bp.browser_context(bp.INTERNAL) as ctx:
            captured = ctx
            raise ValueError("render blew up")
    assert captured.closed is True
    assert bp.stats()["open_contexts"] == 0


@pytest.mark.asyncio
async def test_an_unknown_profile_falls_back_to_internal(_reset_pool):
    await bp.get_browser("nonsense")
    assert set(bp._browsers) == {bp.INTERNAL}


def test_register_sweep_hook_does_not_duplicate():
    async def _h():
        pass

    before = len(bp._sweep_hooks)
    bp.register_sweep_hook(_h)
    bp.register_sweep_hook(_h)
    try:
        assert len(bp._sweep_hooks) == before + 1
    finally:
        bp._sweep_hooks.remove(_h)
