"""Playwright-based web scraper.

Uses headless Chromium to render pages and extract structured content.
Handles both static and JS-heavy sites. Captures page screenshot.

Enhancements:
- Accept-Language: en-US header (helps with region-gated content)
- Cookie/consent banner dismissal (common selectors)
- Cloudflare challenge detection with a short retry
- Scroll to bottom to trigger lazy-loaded content
- Network-event image capture: record every image/* the browser actually
  fetched during render. The DOM extractor sees only `<img>` tags, but
  SPAs frequently paint images via CSS background / canvas / JSON-driven
  rendering. The browser knows what was loaded; we listen for it directly.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from urllib.parse import urlparse

from app.agents.adzump.agents.product.adapters.html_parser import parse_html
from app.agents.adzump.agents.product.models import ScrapeResult, ScrapeTimings

logger = logging.getLogger(__name__)

MAX_CONCURRENT_BROWSERS = 3
NAVIGATION_TIMEOUT_MS = 30_000  # primary goto() ceiling — domcontentloaded usually fires in 1-3s
NETWORKIDLE_SETTLE_MS = 5_000   # best-effort post-load wait; timeout is non-fatal
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Network-image capture filters. Image responses below this size are mostly
# tracking pixels / decorative sprites. content-length headers are usually
# present; when missing we keep the candidate (filter is best-effort).
MIN_IMAGE_BYTES = 2048
MIN_GIF_BYTES = 20 * 1024  # animated GIFs are rare on SMB sites — most GIFs are tracking

# Hosts that serve tracking / analytics / ad imagery. Drop their responses
# even if they pass the size filter.
_AD_TRACKING_HOSTS: frozenset[str] = frozenset({
    "doubleclick.net", "googletagmanager.com", "google-analytics.com",
    "googleadservices.com", "googlesyndication.com",
    "facebook.com", "facebook.net", "fbcdn.net",
    "scorecardresearch.com", "quantserve.com", "amazon-adsystem.com",
    "clarity.ms", "hotjar.com", "linkedin.com",
    "bing.com", "bat.bing.com",
})

# URL path substrings that scream "pixel/beacon/tracking" regardless of host.
_PIXEL_PATH_TOKENS: tuple[str, ...] = (
    "/pixel", "/track", "/beacon", "/collect", "/p.gif", "/__utm",
)


# Common cookie/consent accept buttons — tried in order.
COOKIE_CONSENT_SELECTORS = [
    'button:has-text("Accept all")',
    'button:has-text("Accept All")',
    'button:has-text("Accept")',
    'button:has-text("I agree")',
    'button:has-text("Got it")',
    'button:has-text("Allow all")',
    'button:has-text("OK")',
    '[aria-label="Accept cookies"]',
    '[id*="cookie"] button',
    '[class*="cookie"] button[class*="accept"]',
]

_browser_semaphore = asyncio.Semaphore(MAX_CONCURRENT_BROWSERS)

# Cold-start tracking: the first scrape in a process pays a cold Chromium launch
# (binary/OS-cache warmup). Flagged so steady-state percentiles can exclude it.
_first_scrape_done = False


def _claim_cold_start() -> bool:
    global _first_scrape_done
    if not _first_scrape_done:
        _first_scrape_done = True
        return True
    return False


def _safe_set(d, key, value) -> None:
    """Record a timing value. Never raises into the scrape path."""
    try:
        if d is not None:
            d[key] = value
    except Exception:
        pass


def _ms(start: float) -> float:
    return round((time.monotonic() - start) * 1000, 1)


def _build_timings(raw: dict, t_start: float) -> "ScrapeTimings | None":
    """Finalize the raw stage dict into a ScrapeTimings. Returns None (never
    raises) if anything is malformed — timing must never break a scrape."""
    try:
        data = dict(raw or {})
        total = _ms(t_start)
        data["total_ms"] = total
        sem = data.get("sem_wait_ms") or 0.0
        data["intrinsic_ms"] = round(total - sem, 1)
        return ScrapeTimings(**{k: v for k, v in data.items()
                                if k in ScrapeTimings.model_fields})
    except Exception as e:
        logger.debug("build_timings_failed: %s", str(e)[:120])
        return None


async def scrape_page(
    url: str, on_progress=None, on_early_screenshot=None, on_early_html=None,
) -> ScrapeResult:
    """Scrape a single URL using headless Chromium.

    Returns a ScrapeResult with parsed content and the post-scroll screenshot.
    `on_progress(msg)` is awaited at major stages. `on_early_screenshot(b64)`
    — when provided — is fired with a top-of-page screenshot taken immediately
    after DOM ready. `on_early_html(page)` — when provided — is fired with the
    parsed DOM-ready Page so callers can kick off summary generation in
    parallel with the slow scroll. Asset selection still uses the post-scroll
    Page returned via the normal return value. Parse failures suppress the
    callback rather than propagate.
    """
    timings: dict = {}
    t_start = time.monotonic()
    try:
        html, screenshot_b64, network_images, image_positions = await _fetch_page(
            url, on_progress, on_early_screenshot, on_early_html, timings,
        )
        t_parse = time.monotonic()
        content = parse_html(
            url, html,
            network_images=network_images,
            image_positions=image_positions,
        )
        _safe_set(timings, "parse_ms", _ms(t_parse))
        if not content.title and not content.headings and not content.paragraphs:
            return ScrapeResult(
                success=False,
                error="No meaningful content extracted from page.",
                timings=_build_timings(timings, t_start),
            )
        return ScrapeResult(
            success=True, content=content, screenshot=screenshot_b64,
            timings=_build_timings(timings, t_start),
        )
    except Exception as e:
        logger.error("scrape_failed: url=%s error=%s", url, str(e)[:300])
        return ScrapeResult(
            success=False, error=str(e)[:300],
            timings=_build_timings(timings, t_start),
        )


async def _emit(on_progress, msg: str) -> None:
    if not on_progress:
        return
    try:
        await on_progress(msg)
    except Exception:
        pass


async def _fetch_page(
    url: str, on_progress=None, on_early_screenshot=None, on_early_html=None,
    timings=None,
) -> tuple[str, str | None, list[dict], dict[str, dict]]:
    """Fetch HTML, screenshot, image responses, and per-image rendered positions.

    Returns (html, base64_screenshot, network_images, image_positions).
    `network_images` is a list of `{url, content_type, size}` dicts for image/*
    responses the browser actually fetched — load-bearing for SPAs whose DOM
    has no `<img>` tags. `image_positions` maps image URL → rendered bounding
    box so `_is_header_visual` can identify framework-site header logos."""
    from playwright.async_api import async_playwright

    # sem_wait: time blocked on the cap-3 _browser_semaphore. The harness's outer
    # queue_wait_s only sees the harness Semaphore; this inner wait would otherwise
    # be billed to "work". Measuring it here is the panel's P0 de-confound.
    _t_entry = time.monotonic()
    async with _browser_semaphore:
        _safe_set(timings, "sem_wait_ms", _ms(_t_entry))
        _safe_set(timings, "cold_start", _claim_cold_start())
        _t_launch = time.monotonic()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 800})
                await page.set_extra_http_headers({
                    "User-Agent": USER_AGENT,
                    "Accept-Language": "en-US,en;q=0.9",
                })

                # Wire image-response capture BEFORE goto so we don't miss the
                # document-load wave of image fetches. Listener stores only
                # primitives — never the Response object (which pins the CDP
                # session and can leak across browser.close()).
                captured: dict[str, dict] = {}
                main_frame = page.main_frame

                def _on_response(resp):
                    try:
                        headers = resp.headers or {}
                        ct = headers.get("content-type", "").lower().split(";")[0].strip()
                        if not ct.startswith("image/"):
                            return
                        resp_url = resp.url or ""
                        if not resp_url or resp_url.startswith(("data:", "blob:")):
                            return
                        if resp_url in captured:
                            return
                        # Cross-frame guard: skip iframe responses (ads, embedded widgets).
                        try:
                            if resp.frame and resp.frame is not main_frame:
                                return
                        except Exception:
                            pass
                        # Path-based tracking filter.
                        lower_url = resp_url.lower()
                        if any(token in lower_url for token in _PIXEL_PATH_TOKENS):
                            return
                        # Host-based tracking/ad-network filter.
                        host = (urlparse(resp_url).netloc or "").lower().removeprefix("www.")
                        if any(host == h or host.endswith("." + h) for h in _AD_TRACKING_HOSTS):
                            return
                        # Size filter — known small images are noise; unknown size kept.
                        size_raw = headers.get("content-length")
                        try:
                            size = int(size_raw) if size_raw else None
                        except (TypeError, ValueError):
                            size = None
                        if size is not None and size < MIN_IMAGE_BYTES:
                            return
                        if ct == "image/gif" and (size is None or size < MIN_GIF_BYTES):
                            return
                        captured[resp_url] = {
                            "url": resp_url,
                            "content_type": ct,
                            "size": size,
                            "status": resp.status,
                        }
                    except Exception:
                        # Listener errors must never propagate — they would tear
                        # down the page mid-render.
                        pass

                page.on("response", _on_response)
                _safe_set(timings, "launch_ms", _ms(_t_launch))

                try:
                    # Primary wait: DOM parsed. Real-world ad-pixels / chat widgets /
                    # analytics beacons keep the network "busy" indefinitely on many
                    # sites — ``networkidle`` would wedge us at the 60s timeout for
                    # no content gain. Wait for HTML, then politely give the network
                    # up to 5s more (errors swallowed).
                    # on_progress fires canonical stage names ("fetch", "read",
                    # "capture", "scroll") — the caller translates to user
                    # messages. Keeps this adapter free of tool-specific copy.
                    await _emit(on_progress, "fetch")
                    _t = time.monotonic()
                    response = await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=NAVIGATION_TIMEOUT_MS,
                    )
                    _safe_set(timings, "goto_ms", _ms(_t))
                    if response and response.status in (403, 429, 503):
                        raise RuntimeError(
                            f"HTTP {response.status}: Access denied by server."
                        )
                    await _emit(on_progress, "read")
                    _t = time.monotonic()
                    try:
                        await page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_SETTLE_MS)
                    except Exception:
                        pass  # networkidle never fires on tracker-heavy sites — fine.
                    _safe_set(timings, "networkidle_ms", _ms(_t))

                    # Cloudflare / interstitial challenge — wait briefly and retry content.
                    _t = time.monotonic()
                    await _handle_cloudflare_challenge(page, url)
                    _safe_set(timings, "cloudflare_ms", _ms(_t))

                    # Dismiss common cookie/consent banners (best-effort).
                    _t = time.monotonic()
                    await _dismiss_cookie_banner(page)
                    _safe_set(timings, "cookie_ms", _ms(_t))

                    # Early artifacts: top-of-page screenshot + DOM-ready HTML.
                    # Both fire callbacks so the caller can show a screenshot at
                    # t≈3s and start summary generation in parallel with scroll.
                    # (~0 in the eval harness, which passes no early callbacks.)
                    _t = time.monotonic()
                    if on_early_screenshot:
                        await _emit(on_progress, "capture")
                        try:
                            early_bytes = await page.screenshot(type="jpeg", quality=75)
                            await on_early_screenshot(
                                base64.b64encode(early_bytes).decode("ascii"),
                            )
                        except Exception as e:
                            logger.debug("early_screenshot_failed: %s", str(e)[:100])
                    if on_early_html:
                        try:
                            early_html = await page.content()
                            early_page = parse_html(url, early_html, network_images=[])
                            await on_early_html(early_page)
                        except Exception as e:
                            logger.debug("early_html_failed: %s", str(e)[:100])

                    _safe_set(timings, "early_ms", _ms(_t))

                    # Scroll to bottom to trigger lazy-loaded content, then back to top.
                    await _emit(on_progress, "scroll")
                    _t = time.monotonic()
                    await _scroll_full_page(page, timings)
                    _safe_set(timings, "scroll_ms", _ms(_t))

                    html = await page.content()

                    # Capture bounding-box of every visible <img> AFTER scroll-back-
                    # to-top. The HTML parser can't tell visual position from semantic
                    # HTML alone (modern frameworks ship <div class="comp"> instead
                    # of <header>/<nav>), so we hand it the rendered geometry. Used
                    # downstream to recognize header logos by position.
                    image_positions: dict[str, dict] = {}
                    _t = time.monotonic()
                    try:
                        image_positions = await page.evaluate("""() => {
                            const out = {};
                            for (const img of document.querySelectorAll('img')) {
                                const src = img.currentSrc || img.src;
                                if (!src || src.startsWith('data:') || src.startsWith('blob:')) continue;
                                const r = img.getBoundingClientRect();
                                if (r.width === 0 || r.height === 0) continue;
                                if (out[src]) continue;
                                out[src] = {
                                    top: Math.round(r.top),
                                    left: Math.round(r.left),
                                    width: Math.round(r.width),
                                    height: Math.round(r.height),
                                };
                            }
                            return out;
                        }""") or {}
                    except Exception as e:
                        logger.debug("image_positions_failed: %s", str(e)[:120])
                    _safe_set(timings, "positions_ms", _ms(_t))

                    # Final screenshot AFTER scroll.
                    # Shift 2 (post-v7 redesign · 2026-05-21): capture FULL PAGE
                    # (was viewport-only), then downscale to ≤2000 px long-edge so
                    # downstream consumers (asset picker vision LLM, summary call,
                    # storage upload) receive a predictable payload size. The
                    # screenshot gives the vision picker spatial context: header
                    # strip = logos, partner footer strip = not logos, hero
                    # band = creative, two-logo lockup at top = dual-logo pick.
                    screenshot_b64 = None
                    _t = time.monotonic()
                    try:
                        screenshot_bytes = await page.screenshot(
                            type="jpeg", quality=75, full_page=True,
                        )
                        # Downscale to ≤2000 px long-edge to keep payload bounded
                        # (real-estate microsites can render 5000-15000 px tall).
                        # Helper is colocated in product_assets.py for cross-package
                        # reuse — defensive import here to avoid an import cycle if
                        # the adapter is loaded before product_assets is ready.
                        try:
                            from app.agents.adzump.agents.product.product_assets import (
                                _downscale_screenshot_to_jpeg_bytes as _ds_shot,
                            )
                            ds = _ds_shot(screenshot_bytes)
                            if ds:
                                screenshot_bytes = ds
                        except Exception as e:
                            logger.debug("screenshot_downscale_failed: %s", str(e)[:120])
                        screenshot_b64 = base64.b64encode(screenshot_bytes).decode("ascii")
                    except Exception as e:
                        logger.warning("screenshot_failed: url=%s error=%s", url, str(e)[:100])
                    _safe_set(timings, "screenshot_ms", _ms(_t))

                    network_images = list(captured.values())
                    logger.info(
                        "network_images_captured: url=%s count=%d image_positions=%d",
                        url, len(network_images), len(image_positions),
                    )
                    return html, screenshot_b64, network_images, image_positions
                finally:
                    # Detach before close to avoid holding the page via the
                    # listener closure during teardown.
                    try:
                        page.remove_listener("response", _on_response)
                    except Exception:
                        pass
            finally:
                await browser.close()


async def _handle_cloudflare_challenge(page, url: str) -> None:
    """If the page is a Cloudflare challenge, wait briefly for it to resolve."""
    try:
        title = (await page.title()) or ""
    except Exception:
        return
    marker = title.strip().lower()
    if "just a moment" in marker or "attention required" in marker or "checking your browser" in marker:
        logger.info("cloudflare_challenge_detected: url=%s title=%r", url, title[:80])
        try:
            # Give Cloudflare up to 6s to auto-clear.
            await page.wait_for_load_state("networkidle", timeout=6_000)
        except Exception:
            pass


async def _dismiss_cookie_banner(page) -> None:
    """Try a short list of common consent selectors. Best-effort, never raises."""
    for selector in COOKIE_CONSENT_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            if await locator.is_visible(timeout=500):
                await locator.click(timeout=1_500)
                # Small settle delay after clicking.
                await page.wait_for_timeout(400)
                logger.debug("cookie_banner_dismissed: selector=%s", selector)
                return
        except Exception:
            continue


async def _scroll_full_page(page, timings=None) -> None:
    """Scroll viewport-by-viewport with a fixed dwell at each step.

    A fast bulk scroll (400px every 120ms) is too quick for IntersectionObserver
    callbacks to fire — gallery carousels and section-scoped lazy loads only
    trigger when their parent enters viewport AND stays there long enough.

    We can't rely on `networkidle` here: tracker-heavy SPAs (analytics
    heartbeats, chat widgets, GTM) keep the network busy indefinitely, so
    `wait_for_load_state` returns immediately on timeout while images are
    still being requested. A small **fixed** dwell at each viewport gives
    observers time to fire; a follow-up bounded networkidle wait then lets
    triggered image loads complete before we move on.

    Timing split (eval): ``scroll_dwell_ms`` is the fixed-dwell budget — the
    knob we own (steps x DWELL_MS) — vs ``scroll_loadwait_ms``, the
    network-dependent settle. Plus ``step_count`` and ``scroll_height_px`` so
    a long scroll can be attributed to page tallness vs the dwell constant.
    """
    DWELL_MS = 1_200          # fixed wait so IO callbacks fire
    LOAD_WAIT_MS = 1_500       # bounded wait for triggered loads to finish
    POST_SCROLL_SETTLE_MS = 1_000
    dwell_acc = 0.0
    loadwait_acc = 0.0
    steps = 0
    page_height = 0
    try:
        page_height = await page.evaluate("document.body.scrollHeight") or 0
        viewport_h = await page.evaluate("window.innerHeight") or 800
        step = max(400, int(viewport_h * 0.9))
        positions = list(range(0, max(page_height, step) + step, step))
        for y in positions:
            await page.evaluate(f"window.scrollTo(0, {y})")
            _t = time.monotonic()
            await page.wait_for_timeout(DWELL_MS)
            dwell_acc += (time.monotonic() - _t) * 1000
            _t = time.monotonic()
            try:
                await page.wait_for_load_state("networkidle", timeout=LOAD_WAIT_MS)
            except Exception:
                pass
            loadwait_acc += (time.monotonic() - _t) * 1000
            steps += 1
        await page.evaluate("window.scrollTo(0, 0)")
        _t = time.monotonic()
        try:
            await page.wait_for_load_state(
                "networkidle", timeout=POST_SCROLL_SETTLE_MS,
            )
        except Exception:
            pass
        loadwait_acc += (time.monotonic() - _t) * 1000
    except Exception as e:
        logger.debug("scroll_failed: %s", str(e)[:100])
    finally:
        _safe_set(timings, "scroll_dwell_ms", round(dwell_acc, 1))
        _safe_set(timings, "scroll_loadwait_ms", round(loadwait_acc, 1))
        _safe_set(timings, "step_count", steps)
        _safe_set(timings, "scroll_height_px", int(page_height))
