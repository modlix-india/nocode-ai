"""Playwright-based web scraper.

Uses headless Chromium to render pages and extract structured content.
Handles both static and JS-heavy sites. Captures page screenshot.

Enhancements:
- Accept-Language: en-US header (helps with region-gated content)
- Cookie/consent banner dismissal (common selectors)
- Cloudflare challenge detection with a short retry
- Scroll to bottom to trigger lazy-loaded content
"""

import asyncio
import base64
import logging

from app.agents.adzump.agents.product.adapters.html_parser import parse_html
from app.agents.adzump.agents.product.models import ScrapeResult

logger = logging.getLogger(__name__)

MAX_CONCURRENT_BROWSERS = 3
NAVIGATION_TIMEOUT_MS = 60_000
PER_PAGE_BUDGET_MS = 12_000  # soft cap on extra time spent per page
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
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


async def scrape_page(url: str) -> ScrapeResult:
    """Scrape a single URL using headless Chromium.

    Returns a ScrapeResult with parsed content and screenshot on success.
    """
    try:
        html, screenshot_b64 = await _fetch_page(url)
        content = parse_html(url, html)
        if not content.title and not content.headings and not content.paragraphs:
            return ScrapeResult(
                success=False,
                error="No meaningful content extracted from page.",
            )
        return ScrapeResult(success=True, content=content, screenshot=screenshot_b64)
    except Exception as e:
        logger.error("scrape_failed: url=%s error=%s", url, str(e)[:300])
        return ScrapeResult(success=False, error=str(e)[:300])


async def _fetch_page(url: str) -> tuple[str, str | None]:
    """Fetch HTML and screenshot from a URL. Returns (html, base64_screenshot)."""
    from playwright.async_api import async_playwright

    async with _browser_semaphore:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 800})
                await page.set_extra_http_headers({
                    "User-Agent": USER_AGENT,
                    "Accept-Language": "en-US,en;q=0.9",
                })
                response = await page.goto(
                    url,
                    wait_until="networkidle",
                    timeout=NAVIGATION_TIMEOUT_MS,
                )
                if response and response.status in (403, 429, 503):
                    raise RuntimeError(
                        f"HTTP {response.status}: Access denied by server."
                    )

                # Cloudflare / interstitial challenge — wait briefly and retry content.
                await _handle_cloudflare_challenge(page, url)

                # Dismiss common cookie/consent banners (best-effort).
                await _dismiss_cookie_banner(page)

                # Scroll to bottom to trigger lazy-loaded content, then back to top for screenshot.
                await _scroll_full_page(page)

                html = await page.content()

                # Capture screenshot AFTER scroll (from top of page).
                screenshot_b64 = None
                try:
                    screenshot_bytes = await page.screenshot(type="jpeg", quality=75)
                    screenshot_b64 = base64.b64encode(screenshot_bytes).decode("ascii")
                except Exception as e:
                    logger.warning("screenshot_failed: url=%s error=%s", url, str(e)[:100])

                return html, screenshot_b64
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


async def _scroll_full_page(page) -> None:
    """Scroll to bottom in chunks to trigger lazy loads, then back to top."""
    try:
        await page.evaluate(
            """
            async () => {
                await new Promise((resolve) => {
                    let total = 0;
                    const step = Math.max(400, Math.floor(window.innerHeight * 0.9));
                    const timer = setInterval(() => {
                        window.scrollBy(0, step);
                        total += step;
                        if (total >= document.body.scrollHeight) {
                            clearInterval(timer);
                            window.scrollTo(0, 0);
                            resolve();
                        }
                    }, 120);
                });
            }
            """
        )
        # Settle any lazy-loaded XHRs triggered by scrolling.
        try:
            await page.wait_for_load_state("networkidle", timeout=2_000)
        except Exception:
            pass
    except Exception as e:
        logger.debug("scroll_failed: %s", str(e)[:100])
