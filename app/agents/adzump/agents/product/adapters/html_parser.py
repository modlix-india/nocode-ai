"""Shared HTML parsing utility.

Extracts structured content from raw HTML.
Captures all visible text, not just <p> tags.
"""

import json
import logging
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag, NavigableString

from app.agents.adzump.agents.product.models import PageContent, SiteLink, SiteImage

logger = logging.getLogger(__name__)

# Tags to skip when extracting visible text
_SKIP_TAGS = {"script", "style", "noscript", "svg", "path", "meta", "link", "head"}


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

    # Extract paragraphs from <p> tags
    paragraphs = [
        p.get_text(strip=True) for p in soup.find_all("p") if p.get_text(strip=True)
    ]

    # Also extract text from <li>, <span>, <div> that contain direct text
    # This catches content not wrapped in <p> tags (modern frameworks)
    extra_text = _extract_visible_text(soup)
    if extra_text:
        # Add as additional paragraphs if they contain info not in existing paragraphs
        existing_text = " ".join(paragraphs).lower()
        for text in extra_text:
            if text.lower() not in existing_text and len(text) > 30:
                paragraphs.append(text)

    links: list[SiteLink] = []
    seen_hrefs: set[str] = set()
    for a in soup.find_all("a", href=True):
        if not isinstance(a, Tag):
            continue
        href = a.get("href")
        if not isinstance(href, str) or not href:
            continue
        href = href.strip()
        if not href or href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        links.append(SiteLink(text=a.get_text(strip=True), href=href))

    images: list[SiteImage] = []

    for img in soup.find_all("img"):
        if not isinstance(img, Tag):
            continue

        src = img.get("src")
        if not src or not isinstance(src, str):
            continue

        src = urljoin(url, src.strip())

        if not src:
            continue

        alt = str(img.get("alt", "")).strip()
        image_title = str(img.get("title", "")).strip()

        width = None
        height = None

        try:
            width = int(img.get("width")) if img.get("width") else None
        except Exception:
            pass

        try:
            height = int(img.get("height")) if img.get("height") else None
        except Exception:
            pass

        images.append(
            SiteImage(
                src=src,
                alt=alt,
                title=image_title,
                width=width,
                height=height,
            )
        )

    structured_data = _extract_json_ld(soup)

    logo_url = _extract_logo(images)

    return PageContent(
        url=url,
        title=title,
        meta_description=meta_description,
        headings=headings,
        paragraphs=paragraphs,
        links=links,
        images=images,
        logo_url=logo_url,
        structured_data=structured_data,
    )


def _extract_logo(images: list[SiteImage]) -> str | None:
    logo_keywords = [
        "logo",
        "brand",
        "navbar",
        "header",
        "site-logo",
        "brand-logo",
        "company-logo",
    ]

    for image in images:
        combined = (f"{image.src} {image.alt} {image.title}").lower()

        if any(keyword in combined for keyword in logo_keywords):
            return image.src

    return None


def _extract_visible_text(soup: BeautifulSoup) -> list[str]:
    """Extract meaningful visible text blocks from the page body."""
    body = soup.find("body")
    if not body:
        return []

    texts: list[str] = []
    seen: set[str] = set()

    for element in body.find_all(
        ["div", "section", "article", "li", "td", "blockquote"]
    ):
        if element.name in _SKIP_TAGS:
            continue

        # Get direct text (not from child block elements)
        direct_text = ""
        for child in element.children:
            if isinstance(child, NavigableString):
                direct_text += child.strip() + " "
            elif isinstance(child, Tag) and child.name in (
                "span",
                "strong",
                "em",
                "b",
                "i",
                "a",
                "br",
            ):
                direct_text += child.get_text(strip=True) + " "

        direct_text = direct_text.strip()
        if direct_text and len(direct_text) > 20 and direct_text not in seen:
            seen.add(direct_text)
            texts.append(direct_text)

    return texts


def _extract_json_ld(soup: BeautifulSoup) -> dict | None:
    """Extract JSON-LD structured data from the page."""
    script = soup.find("script", attrs={"type": "application/ld+json"})
    if not isinstance(script, Tag) or not script.string:
        return None
    try:
        return json.loads(script.string)
    except (json.JSONDecodeError, TypeError):
        return None
