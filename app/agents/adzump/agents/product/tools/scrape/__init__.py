"""Scrape tool package - Playwright-based page scraping for the ProductAgent.

Public surface: the `scrape_url` ToolDefinition. Internals (profile, assets,
receipts) are package-private; consumers should only import `scrape_url`.
"""

from app.agents.adzump.agents.product.tools.scrape.tool import scrape_url

__all__ = ["scrape_url"]
