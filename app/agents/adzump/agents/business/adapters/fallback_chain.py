"""Fallback scraping chain — tries adapters in order.

Ported from ds/adapters/scraping/fallback_chain.py.
"""

import logging

from app.agents.adzump.agents.business.adapters.base import ScrapingAdapter
from app.agents.adzump.agents.business.models import ScrapeResult

logger = logging.getLogger(__name__)


class FallbackScrapingChain(ScrapingAdapter):
    """Tries adapters in order, returning first successful result."""

    def __init__(self, adapters: list[ScrapingAdapter]):
        self._adapters = adapters

    async def scrape(self, url: str) -> ScrapeResult:
        result = ScrapeResult(success=False, error="No adapters configured.")
        for adapter in self._adapters:
            adapter_name = type(adapter).__name__
            logger.info("scrape_trying: adapter=%s url=%s", adapter_name, url)
            result = await adapter.scrape(url)
            if result.success:
                logger.info("scrape_success: adapter=%s url=%s", adapter_name, url)
                return result
            logger.info("scrape_failed: adapter=%s url=%s error=%s", adapter_name, url, result.error)
        return result
