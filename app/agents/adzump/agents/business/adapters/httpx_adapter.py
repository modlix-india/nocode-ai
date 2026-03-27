"""Lightweight HTTP-only scraper for static sites.

Uses httpx to fetch HTML and BeautifulSoup to parse. Detects JS-heavy pages
and returns failure so the fallback chain can try Playwright.

Ported from ds/adapters/scraping/httpx_adapter.py.
"""

import logging

import httpx
from bs4 import BeautifulSoup

from app.agents.adzump.agents.business.adapters.base import ScrapingAdapter
from app.agents.adzump.agents.business.adapters.html_parser import parse_html
from app.agents.adzump.agents.business.models import ScrapeResult

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
)

JS_HEAVY_INDICATORS = [
    "enable javascript",
    "requires javascript",
    "please enable javascript",
    "noscript",
    "this app works best with javascript enabled",
    "__next",
    "window.__INITIAL_STATE__",
]


class HttpxScrapingAdapter(ScrapingAdapter):
    """Lightweight HTTP-only scraper for static sites."""

    async def scrape(self, url: str) -> ScrapeResult:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    url,
                    headers={"User-Agent": USER_AGENT},
                    follow_redirects=True,
                )
                response.raise_for_status()
        except Exception as e:
            logger.warning("httpx_scrape_fetch_failed: url=%s error=%s", url, str(e)[:200])
            return ScrapeResult(success=False, error=f"HTTP fetch failed: {str(e)[:200]}")

        html = response.text
        if self._is_js_heavy(html):
            logger.info("httpx_js_heavy_page: url=%s", url)
            return ScrapeResult(
                success=False,
                error="Page requires JavaScript rendering.",
            )

        content = parse_html(url, html)
        if not content.title and not content.headings and not content.paragraphs:
            return ScrapeResult(
                success=False, error="No meaningful content extracted."
            )

        return ScrapeResult(success=True, content=content)

    def _is_js_heavy(self, html: str) -> bool:
        html_lower = html.lower()
        body_start = html_lower.find("<body")
        if body_start == -1:
            return True
        body_text = html_lower[body_start:]
        text_len = len(BeautifulSoup(body_text, "html.parser").get_text(strip=True))
        if text_len < 100:
            for indicator in JS_HEAVY_INDICATORS:
                if indicator in html_lower:
                    return True
        return False
