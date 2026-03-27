"""Playwright-based scraper for JS-heavy sites.

Uses headless Chromium for full page rendering. Falls back from httpx
when JavaScript rendering is required.

Ported from ds/adapters/scraping/playwright_adapter.py.
"""

import asyncio
import logging

from app.agents.adzump.agents.business.adapters.base import ScrapingAdapter
from app.agents.adzump.agents.business.adapters.html_parser import parse_html
from app.agents.adzump.agents.business.models import ScrapeResult

logger = logging.getLogger(__name__)

MAX_CONCURRENT_BROWSERS = 3
NAVIGATION_TIMEOUT_MS = 60_000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_browser_semaphore = asyncio.Semaphore(MAX_CONCURRENT_BROWSERS)


class PlaywrightScrapingAdapter(ScrapingAdapter):
    """Scrapes pages using headless Chromium via Playwright."""

    async def scrape(self, url: str) -> ScrapeResult:
        try:
            html = await self._fetch_html(url)
            content = parse_html(url, html)
            if not content.title and not content.headings and not content.paragraphs:
                return ScrapeResult(
                    success=False,
                    error="No meaningful content extracted from page.",
                )
            return ScrapeResult(success=True, content=content)
        except Exception as e:
            logger.error("playwright_scrape_failed: url=%s error=%s", url, str(e)[:300])
            return ScrapeResult(success=False, error=str(e)[:300])

    async def _fetch_html(self, url: str) -> str:
        from playwright.async_api import async_playwright

        async with _browser_semaphore:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.set_extra_http_headers({"User-Agent": USER_AGENT})
                    response = await page.goto(
                        url,
                        wait_until="networkidle",
                        timeout=NAVIGATION_TIMEOUT_MS,
                    )
                    if response and response.status in (403, 429, 503):
                        raise RuntimeError(
                            f"HTTP {response.status}: Access denied by server."
                        )
                    return await page.content()
                finally:
                    await browser.close()
