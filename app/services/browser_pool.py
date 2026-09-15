"""Process-shared headless Chromium, so a render costs a tab instead of a browser.

Every Playwright call site used to do `async_playwright().start()` +
`chromium.launch()` per call. That spawns a node driver plus a whole Chromium
process tree before a single pixel is drawn. Measured on dev (15 concurrent
browsers, RSS per browser):

    node driver        73 MB
    chromium main      90 MB
    gpu process        66 MB
    network utility    80 MB
    2 zygotes          70 MB
    ---------------------------
    fixed overhead    379 MB   <- thrown away after one page
    renderer          216 MB   <- per open page, unavoidable

Chromium allocates one renderer per page and never shares renderers across
contexts (a different context is a different profile, so a different browsing
instance). So the 216 MB is a floor no amount of pooling removes, while the
379 MB is pure waste repeated per call.

This module keeps the browser alive and hands out BrowserContexts instead.
A context is the isolation boundary that actually matters: its own cookies,
localStorage, cache, permissions, extra headers and init scripts. It costs
nothing until a page is opened in it.

TWO PROFILES, not one. Playwright launches Chromium with `--no-sandbox`
(`chromiumSandbox` defaults to false) and we render untrusted third-party
sites for cloning. INTERNAL renders Modlix pages carrying real end-user
tokens; EXTERNAL renders whatever URL a clone or scrape task was pointed at.
Storage is isolated either way, but this also keeps them out of one shared
unsandboxed process tree.

Browsers are closed once they have been idle with zero contexts for
BROWSER_IDLE_TTL, so a quiet worker holds no Chromium at all. Without that
the fixed cost would simply become permanently resident instead of repeated.

Scope: this is per worker process, and gunicorn runs four. A single pool
across workers means running Chromium as its own service and using
`browser_type.connect()`; this is the in-process version of that.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable

logger = logging.getLogger(__name__)


INTERNAL = "internal"
EXTERNAL = "external"
_PROFILES = (INTERNAL, EXTERNAL)


class BrowserUnavailable(RuntimeError):
    """Playwright is not installed, or Chromium could not be launched."""


def _cfg(name: str, default: Any) -> Any:
    try:
        from app.config import settings
    except Exception:  # noqa: BLE001
        return default
    value = getattr(settings, name, None)
    return default if value is None else value


# ── Process-wide state ───────────────────────────────────────────────────
#
# One uvicorn worker is one event loop, so plain module globals are the right
# scope here. `_loop` guards the case where the loop is replaced under us
# (tests, a re-exec): every asyncio primitive below binds to the loop that
# first awaits it, so they all have to be rebuilt together.

_loop: asyncio.AbstractEventLoop | None = None
_playwright: Any = None
_browsers: dict[str, Any] = {}
_browser_last_used: dict[str, float] = {}
_launch_lock: asyncio.Lock | None = None
_context_sem: asyncio.Semaphore | None = None
_open_contexts: dict[int, str] = {}  # id(context) -> profile
_sweep_task: asyncio.Task | None = None
_sweep_hooks: list[Callable[[], Awaitable[Any]]] = []

# Counters, for stats() only.
_stat_launches = 0
_stat_contexts = 0
_stat_relaunches = 0


def _ensure_loop() -> None:
    """Rebuild loop-bound primitives if the running loop changed."""
    global _loop, _launch_lock, _context_sem
    running = asyncio.get_running_loop()
    if _loop is running and _launch_lock is not None and _context_sem is not None:
        return
    _loop = running
    _launch_lock = asyncio.Lock()
    _context_sem = asyncio.Semaphore(int(_cfg("BROWSER_MAX_CONTEXTS", 6)))
    _open_contexts.clear()


def register_sweep_hook(hook: Callable[[], Awaitable[Any]]) -> None:
    """Register a coroutine run before each idle-browser sweep.

    Callers that hold contexts across calls (drive_page sessions) register
    their own reaper here, so contexts are released BEFORE we decide whether a
    browser is idle. Otherwise a stale session would keep its browser alive
    forever.
    """
    if hook not in _sweep_hooks:
        _sweep_hooks.append(hook)


# ── Browser lifecycle ────────────────────────────────────────────────────


async def _get_playwright() -> Any:
    global _playwright
    if _playwright is not None:
        return _playwright
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:  # noqa: BLE001
        raise BrowserUnavailable(
            "playwright not installed; pip install playwright && "
            "python -m playwright install chromium"
        ) from e
    _playwright = await async_playwright().start()
    logger.info("Playwright driver started")
    return _playwright


async def get_browser(profile: str = INTERNAL) -> Any:
    """Return the shared Browser for `profile`, launching it if needed.

    Relaunches if the previous browser died: Chromium can be killed out from
    under us (cgroup OOM, a crashed page taking the tree with it) and
    `is_connected()` is the only honest way to find out before we try to use it.
    """
    global _stat_launches, _stat_relaunches
    if profile not in _PROFILES:
        profile = INTERNAL
    _ensure_loop()

    existing = _browsers.get(profile)
    if existing is not None and existing.is_connected():
        _browser_last_used[profile] = time.monotonic()
        return existing

    assert _launch_lock is not None
    async with _launch_lock:
        # Re-check: another coroutine may have launched while we waited.
        existing = _browsers.get(profile)
        if existing is not None and existing.is_connected():
            _browser_last_used[profile] = time.monotonic()
            return existing
        if existing is not None:
            _stat_relaunches += 1
            logger.warning("Browser '%s' was disconnected; relaunching", profile)
            _browsers.pop(profile, None)

        pw = await _get_playwright()
        try:
            browser = await pw.chromium.launch()
        except Exception as e:  # noqa: BLE001
            raise BrowserUnavailable(f"chromium launch failed: {type(e).__name__}: {e}") from e
        _browsers[profile] = browser
        _browser_last_used[profile] = time.monotonic()
        _stat_launches += 1
        logger.info("Launched shared browser '%s' (launch #%d)", profile, _stat_launches)
        return browser


async def _close_browser(profile: str) -> None:
    browser = _browsers.pop(profile, None)
    _browser_last_used.pop(profile, None)
    if browser is None:
        return
    try:
        await browser.close()
    except Exception:  # noqa: BLE001
        logger.exception("error closing shared browser '%s'", profile)


# ── Contexts ─────────────────────────────────────────────────────────────


async def open_context(profile: str = INTERNAL, **kwargs: Any) -> Any:
    """Acquire a permit and open an isolated BrowserContext.

    The caller MUST pass the result to `close_context()`, or use the
    `browser_context()` wrapper which does it for you. Every open context holds
    a renderer once a page is opened in it, so leaking one leaks ~216 MB.
    """
    global _stat_contexts
    _ensure_loop()
    assert _context_sem is not None

    timeout = float(_cfg("BROWSER_ACQUIRE_TIMEOUT_SECONDS", 120))
    try:
        await asyncio.wait_for(_context_sem.acquire(), timeout=timeout)
    except asyncio.TimeoutError as e:
        raise BrowserUnavailable(
            f"no browser context free after {timeout:.0f}s "
            f"({len(_open_contexts)} in use, cap {_cfg('BROWSER_MAX_CONTEXTS', 6)})"
        ) from e

    try:
        browser = await get_browser(profile)
        ctx = await browser.new_context(**kwargs)
    except BaseException:
        _context_sem.release()
        raise

    _open_contexts[id(ctx)] = profile
    _stat_contexts += 1
    return ctx


async def close_context(ctx: Any) -> None:
    """Close a context and release its permit. Safe to call twice."""
    if ctx is None:
        return
    profile = _open_contexts.pop(id(ctx), None)
    try:
        await ctx.close()
    except Exception:  # noqa: BLE001
        # A context whose browser already died raises here. The permit still
        # has to come back or the cap leaks downward until the worker restarts.
        logger.debug("error closing browser context", exc_info=True)
    if profile is not None:
        _browser_last_used[profile] = time.monotonic()
        assert _context_sem is not None
        _context_sem.release()


@asynccontextmanager
async def browser_context(
    profile: str = INTERNAL, *, headless: bool = True, **kwargs: Any,
) -> AsyncIterator[Any]:
    """Scoped context for one-shot renders (screenshots, scrapes, analysis).

        async with browser_context(viewport={"width": 1440, "height": 900}) as ctx:
            page = await ctx.new_page()
            ...

    `headless=False` bypasses the pool and launches a throwaway headful browser.
    That is a developer watching a window on their own machine, and it must not
    be pooled: the next unrelated caller would inherit the visible window.
    """
    if not headless:
        pw = await _get_playwright()
        browser = await pw.chromium.launch(headless=False)
        try:
            yield await browser.new_context(**kwargs)
        finally:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                logger.exception("error closing headful debug browser")
        return

    ctx = await open_context(profile, **kwargs)
    try:
        yield ctx
    finally:
        await close_context(ctx)


# ── Maintenance ──────────────────────────────────────────────────────────


async def _sweep_once() -> None:
    # Session reapers first: they release contexts, which is what decides
    # whether a browser counts as idle.
    for hook in list(_sweep_hooks):
        try:
            await hook()
        except Exception:  # noqa: BLE001
            logger.exception("browser pool sweep hook failed")

    ttl = float(_cfg("BROWSER_IDLE_TTL_SECONDS", 300))
    if ttl <= 0:
        return
    now = time.monotonic()
    in_use = set(_open_contexts.values())
    for profile in list(_browsers):
        if profile in in_use:
            continue
        last = _browser_last_used.get(profile, 0.0)
        if now - last > ttl:
            logger.info(
                "Closing idle shared browser '%s' (idle %.0fs, no live contexts)",
                profile, now - last,
            )
            await _close_browser(profile)


async def _sweep_loop() -> None:
    interval = max(15.0, float(_cfg("BROWSER_SWEEP_INTERVAL_SECONDS", 60)))
    while True:
        try:
            await asyncio.sleep(interval)
            await _sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("browser pool sweep failed")


def start_maintenance() -> None:
    """Start the background sweeper. Idempotent; call from app startup.

    This is the piece that makes cleanup unconditional. Reaping lazily inside
    a tool call means a worker that stops taking calls keeps its browsers
    forever, which is exactly how twelve of them sat idle for three hours.
    """
    global _sweep_task
    if _sweep_task is not None and not _sweep_task.done():
        return
    _ensure_loop()
    _sweep_task = asyncio.create_task(_sweep_loop(), name="browser-pool-sweep")
    logger.info("Browser pool maintenance started")


async def close_all() -> int:
    """Close every context and browser. Returns browsers closed."""
    global _sweep_task, _playwright
    if _sweep_task is not None:
        _sweep_task.cancel()
        try:
            await _sweep_task
        except asyncio.CancelledError:
            pass  # expected: we just cancelled it
        except Exception:  # noqa: BLE001
            logger.exception("browser pool sweep task ended badly")
        _sweep_task = None

    for hook in list(_sweep_hooks):
        try:
            await hook()
        except Exception:  # noqa: BLE001
            logger.exception("browser pool shutdown hook failed")

    closed = 0
    for profile in list(_browsers):
        await _close_browser(profile)
        closed += 1
    _open_contexts.clear()

    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception:  # noqa: BLE001
            logger.exception("error stopping playwright driver")
        _playwright = None
    return closed


def stats() -> dict[str, Any]:
    now = time.monotonic()
    return {
        "browsers": [
            {
                "profile": p,
                "connected": bool(b.is_connected()),
                "idle_seconds": round(now - _browser_last_used.get(p, now), 1),
                "contexts": sum(1 for v in _open_contexts.values() if v == p),
            }
            for p, b in _browsers.items()
        ],
        "open_contexts": len(_open_contexts),
        "max_contexts": int(_cfg("BROWSER_MAX_CONTEXTS", 6)),
        "total_launches": _stat_launches,
        "total_relaunches": _stat_relaunches,
        "total_contexts": _stat_contexts,
    }
