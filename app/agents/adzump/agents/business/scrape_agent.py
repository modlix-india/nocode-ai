"""ScrapeAgent — orchestrates the scraping pipeline.

Simple linear pipeline:
  1. Scrape URL (httpx → Playwright fallback)
  2. Scrape high-value subpages (contact, about, services)
  3. Extract metadata via LLM (Pass 1)
  4. Extract summary via LLM (Pass 2)
  5. Build BusinessProfile

Simplified from ds/agents/scrape/scrape_agent.py — no storage, no streaming,
no geo resolution, no screenshots.
"""

import logging
from urllib.parse import urlparse

from app.agents.adzump.agents.business.adapters.fallback_chain import FallbackScrapingChain
from app.agents.adzump.agents.business.adapters.httpx_adapter import HttpxScrapingAdapter
from app.agents.adzump.agents.business.adapters.playwright_adapter import PlaywrightScrapingAdapter
from app.agents.adzump.agents.business.extraction_service import ExtractionService
from app.agents.adzump.agents.business.models import BusinessProfile, PageContent

logger = logging.getLogger(__name__)

PRIORITY_PAGE_PATTERNS = ["contact", "about", "locations", "services", "pricing"]
MAX_SUBPAGES = 3


def _select_subpages(links: list[str], base_url: str) -> list[str]:
    """Select high-value subpages from homepage links."""
    base_host = urlparse(base_url).netloc
    selected: list[str] = []
    for link in links:
        parsed = urlparse(link)
        if parsed.netloc and parsed.netloc != base_host:
            continue
        path = parsed.path.lower()
        if any(p in path for p in PRIORITY_PAGE_PATTERNS):
            full_url = link if parsed.netloc else f"{urlparse(base_url).scheme}://{base_host}{parsed.path}"
            if full_url not in selected:
                selected.append(full_url)
    return selected[:MAX_SUBPAGES]


class ScrapeAgent:
    """Orchestrates scraping and LLM extraction into a BusinessProfile."""

    def __init__(self) -> None:
        self._scraper = FallbackScrapingChain([
            HttpxScrapingAdapter(),
            PlaywrightScrapingAdapter(),
        ])
        self._extraction = ExtractionService()

    async def run(self, url: str) -> BusinessProfile:
        """Run the full scraping pipeline for a URL.

        Returns a BusinessProfile with extracted business information.
        Raises RuntimeError if scraping fails completely.
        """
        logger.info("scrape_agent_start: url=%s", url)

        # Step 1: Scrape homepage
        pages: list[PageContent] = []
        result = await self._scraper.scrape(url)
        if not result.success or not result.content:
            raise RuntimeError(f"Failed to scrape {url}: {result.error}")
        pages.append(result.content)
        logger.info("homepage_scraped: title=%s links=%d", result.content.title, len(result.content.links))

        # Step 2: Scrape high-value subpages
        subpage_urls = _select_subpages(result.content.links, url)
        for sub_url in subpage_urls:
            sub_result = await self._scraper.scrape(sub_url)
            if sub_result.success and sub_result.content:
                pages.append(sub_result.content)
                logger.info("subpage_scraped: url=%s", sub_url)

        logger.info("scraping_complete: pages=%d", len(pages))

        # Step 3: Extract metadata (Pass 1 — cheap)
        metadata = await self._extraction.extract_metadata(pages)

        # Step 4: Extract summary (Pass 2 — uses metadata as context)
        summary = await self._extraction.extract_summary(pages, metadata)

        # Step 5: Build profile
        profile = self._extraction.build_profile(metadata, summary, pages)
        logger.info("profile_built: brand=%s type=%s", profile.brand_name, profile.business_type)

        return profile


# Module-level singleton
_scrape_agent: ScrapeAgent | None = None


def get_scrape_agent() -> ScrapeAgent:
    """Get the shared ScrapeAgent singleton."""
    global _scrape_agent
    if _scrape_agent is None:
        _scrape_agent = ScrapeAgent()
    return _scrape_agent
