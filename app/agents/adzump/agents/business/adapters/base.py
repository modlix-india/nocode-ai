"""Abstract base for scraping adapters."""

from abc import ABC, abstractmethod

from app.agents.adzump.agents.business.models import ScrapeResult


class ScrapingAdapter(ABC):
    @abstractmethod
    async def scrape(self, url: str) -> ScrapeResult: ...
