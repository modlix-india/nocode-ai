"""Shared HTML parsing utility.

Extracts structured content from raw HTML — used by both httpx and Playwright adapters.
Ported from ds where this logic was duplicated between adapters.
"""

import json
import logging

from bs4 import BeautifulSoup, Tag

from app.agents.adzump.agents.business.models import PageContent

logger = logging.getLogger(__name__)


def parse_html(url: str, html: str) -> PageContent:
    """Parse HTML into structured PageContent."""
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    meta_desc_tag = soup.find("meta", attrs={"name": "description"})
    meta_description = ""
    if isinstance(meta_desc_tag, Tag):
        meta_description = str(meta_desc_tag.get("content", "")).strip()

    headings: list[str] = []
    for tag_name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        for h in soup.find_all(tag_name):
            text = h.get_text(strip=True)
            if text:
                headings.append(text)

    paragraphs = [
        p.get_text(strip=True)
        for p in soup.find_all("p")
        if p.get_text(strip=True)
    ]

    links: list[str] = []
    for a in soup.find_all("a", href=True):
        if isinstance(a, Tag):
            href = a.get("href")
            if isinstance(href, str) and href:
                links.append(href)

    structured_data = _extract_json_ld(soup)

    return PageContent(
        url=url,
        title=title,
        meta_description=meta_description,
        headings=headings,
        paragraphs=paragraphs,
        links=links,
        structured_data=structured_data,
    )


def _extract_json_ld(soup: BeautifulSoup) -> dict | None:
    """Extract JSON-LD structured data from the page."""
    script = soup.find("script", attrs={"type": "application/ld+json"})
    if not isinstance(script, Tag) or not script.string:
        return None
    try:
        return json.loads(script.string)
    except (json.JSONDecodeError, TypeError):
        return None
