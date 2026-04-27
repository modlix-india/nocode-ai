"""ScrapePipeline — orchestrates the scraping pipeline.

Simple linear pipeline:
  1. Scrape URL via Playwright
  2. Scrape high-value subpages (contact, about, services)
  3. Extract metadata via LLM (Pass 1)
  4. Extract summary via LLM (Pass 2)
  5. Build BusinessProfile
"""

import logging
from typing import Any, Callable, Coroutine
from urllib.parse import urlparse

from app.agents.adzump.agents.product.adapters.playwright_adapter import scrape_page
from app.agents.adzump.agents.product.extraction_service import ExtractionService
from app.agents.adzump.agents.product.models import BusinessProfile, PageContent

# Type for the optional progress callback
ProgressCallback = Callable[[str], Coroutine[Any, Any, None]]
# Type for craft stage callback: (stage, metadata, summary)
CraftCallback = Callable[[str, Any, Any], Coroutine[Any, Any, None]]

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


class ScrapePipeline:
    """Orchestrates scraping and LLM extraction into a BusinessProfile."""

    def __init__(self) -> None:
        self._extraction = ExtractionService()

    async def run(
        self, url: str,
        progress_callback: ProgressCallback | None = None,
        craft_callback: CraftCallback | None = None,
    ) -> BusinessProfile:
        """Run the full scraping pipeline for a URL.

        Args:
            url: Website URL to scrape.
            progress_callback: Optional async callback for tool progress updates.
            craft_callback: Optional async callback for progressive craft panel updates.
                Called with (stage, metadata, summary) at "metadata" and "complete" stages.

        Returns a BusinessProfile with extracted business information.
        Raises RuntimeError if scraping fails completely.
        """
        async def _progress(message: str) -> None:
            if progress_callback:
                await progress_callback(message)

        logger.info("scrape_pipeline_start: url=%s", url)

        # Step 1: Scrape homepage
        await _progress("Scraping homepage...")
        pages: list[PageContent] = []
        result = await scrape_page(url)
        if not result.success or not result.content:
            raise RuntimeError(f"Failed to scrape {url}: {result.error}")
        pages.append(result.content)
        homepage_screenshot = result.screenshot
        page = result.content
        logger.info("homepage_scraped: title=%s links=%d screenshot=%s",
                     page.title, len(page.links),
                     "yes" if homepage_screenshot else "no")

        # Emit screenshot to craft before metadata
        if craft_callback and homepage_screenshot:
            await craft_callback("screenshot", homepage_screenshot, None)

        # Step 2: Scrape high-value subpages
        subpage_urls = _select_subpages(result.content.links, url)
        if subpage_urls:
            await _progress(f"Scraping {len(subpage_urls)} subpages...")
            for sub_url in subpage_urls:
                sub_result = await scrape_page(sub_url)
                if sub_result.success and sub_result.content:
                    pages.append(sub_result.content)
                    logger.info("subpage_scraped: url=%s", sub_url)

        logger.info("scraping_complete: pages=%d", len(pages))

        # Step 3: Extract metadata
        await _progress("Extracting business info...")
        metadata = await self._extraction.extract_metadata(pages)

        if craft_callback:
            await craft_callback("metadata", metadata, None)

        # Step 4: Stream marketing summary
        await _progress("Generating marketing summary...")
        summary_text = ""
        async for token in self._extraction.stream_summary(pages, metadata):
            summary_text += token
            if craft_callback:
                await craft_callback("summary_delta", token, None)

        # Step 5: Build profile
        profile = self._extraction.build_profile(metadata, summary_text, pages)

        if craft_callback:
            await craft_callback("complete", None, None)
        logger.info("profile_built: brand=%s type=%s", profile.product_name, profile.business_type)

        return profile


# Module-level singleton
_scrape_pipeline: ScrapePipeline | None = None


def get_scrape_pipeline() -> ScrapePipeline:
    """Get the shared ScrapePipeline singleton."""
    global _scrape_pipeline
    if _scrape_pipeline is None:
        _scrape_pipeline = ScrapePipeline()
    return _scrape_pipeline
