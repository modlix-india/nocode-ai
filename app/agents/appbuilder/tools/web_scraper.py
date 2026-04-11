"""Website scraper — extracts structure, styles, content, and screenshots from URLs.

Used when a user provides a URL to clone or reference. Extracts:
1. Page structure (sections, navigation, hero, cards, footers)
2. CSS styles (colors, fonts, spacing, backgrounds)
3. Text content and image URLs
4. Screenshot (via Playwright) for visual context

The extracted data is formatted as a structured representation that maps
to Modlix component types, making it easy for the agent to recreate.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)


# ── HTML/CSS Scraping ────────────────────────────────────────────


async def scrape_website(url: str) -> dict[str, Any]:
    """Scrape a website and extract structure, styles, and content.

    Returns a dict with:
        - url: the scraped URL
        - title: page title
        - meta: description, keywords, og:image
        - colors: extracted color palette
        - fonts: font families used
        - sections: list of page sections with components
        - images: list of image URLs
        - navigation: nav links
        - screenshot_base64: viewport screenshot (if Playwright available)
    """
    result: dict[str, Any] = {"url": url, "error": None}

    try:
        html, final_url = await _fetch_html(url)
        result["url"] = final_url
    except Exception as e:
        result["error"] = f"Failed to fetch {url}: {e}"
        return result

    soup = BeautifulSoup(html, "lxml")

    # Extract metadata
    result["title"] = _extract_title(soup)
    result["meta"] = _extract_meta(soup)

    # Extract color palette and fonts from inline/embedded CSS
    styles_text = _extract_all_css(soup, html)
    result["colors"] = _extract_colors(styles_text)
    result["fonts"] = _extract_fonts(styles_text)

    # Extract navigation
    result["navigation"] = _extract_navigation(soup, final_url)

    # Extract page sections
    result["sections"] = _extract_sections(soup, final_url)

    # Extract all images
    result["images"] = _extract_images(soup, final_url)

    # Try to take a screenshot
    try:
        screenshot = await _take_screenshot(url)
        if screenshot:
            result["screenshot_base64"] = screenshot
    except Exception as e:
        logger.debug("Screenshot failed: %s", e)

    return result


async def _fetch_html(url: str) -> tuple[str, str]:
    """Fetch HTML content from a URL. Returns (html, final_url)."""
    async with httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text, str(resp.url)


def _extract_title(soup: BeautifulSoup) -> str:
    tag = soup.find("title")
    return tag.get_text(strip=True) if tag else ""


def _extract_meta(soup: BeautifulSoup) -> dict[str, str]:
    meta = {}
    for tag in soup.find_all("meta"):
        name = tag.get("name", "").lower() or tag.get("property", "").lower()
        content = tag.get("content", "")
        if name in ("description", "keywords", "og:image", "og:title", "og:description"):
            meta[name] = content
    return meta


def _extract_all_css(soup: BeautifulSoup, html: str) -> str:
    """Collect all inline and embedded CSS text."""
    parts: list[str] = []
    # <style> tags
    for style_tag in soup.find_all("style"):
        parts.append(style_tag.get_text())
    # Inline style attributes
    for tag in soup.find_all(style=True):
        parts.append(tag.get("style", ""))
    return "\n".join(parts)


def _extract_colors(css_text: str) -> list[str]:
    """Extract unique color values from CSS."""
    colors: set[str] = set()
    # Hex colors
    for match in re.findall(r"#[0-9a-fA-F]{3,8}\b", css_text):
        colors.add(match.lower())
    # rgb/rgba
    for match in re.findall(r"rgba?\([^)]+\)", css_text):
        colors.add(match)
    # Named colors in common properties
    for match in re.findall(r"(?:color|background(?:-color)?)\s*:\s*([a-zA-Z]+)", css_text):
        if match.lower() not in ("inherit", "initial", "unset", "transparent", "none", "auto"):
            colors.add(match.lower())
    return sorted(colors)[:20]  # Cap at 20


def _extract_fonts(css_text: str) -> list[str]:
    """Extract font families from CSS."""
    fonts: set[str] = set()
    for match in re.findall(r"font-family\s*:\s*([^;]+)", css_text):
        for font in match.split(","):
            font = font.strip().strip("'\"")
            if font and font.lower() not in ("serif", "sans-serif", "monospace", "cursive", "fantasy", "inherit"):
                fonts.add(font)
    return sorted(fonts)[:10]


def _extract_navigation(soup: BeautifulSoup, base_url: str) -> list[dict[str, str]]:
    """Extract navigation links from nav elements or header."""
    nav_links: list[dict[str, str]] = []
    nav = soup.find("nav") or soup.find("header")
    if not nav:
        return nav_links

    for a in nav.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a.get("href", "")
        if text and href and not href.startswith("#"):
            nav_links.append({"text": text, "href": urljoin(base_url, href)})
        elif text and href:
            nav_links.append({"text": text, "href": href})

    return nav_links[:15]


def _extract_sections(soup: BeautifulSoup, base_url: str) -> list[dict[str, Any]]:
    """Extract page sections with structure and content."""
    sections: list[dict[str, Any]] = []

    # Find main content sections
    body = soup.find("body")
    if not body:
        return sections

    # Look for semantic sections or top-level divs
    candidates = body.find_all(["section", "main", "article", "header", "footer"], recursive=False)
    if not candidates:
        # Fallback: top-level divs within body
        candidates = body.find_all("div", recursive=False)

    for i, section in enumerate(candidates[:15]):  # Cap at 15 sections
        sec_data = _extract_section(section, base_url, i)
        if sec_data:
            sections.append(sec_data)

    return sections


def _extract_section(element: Tag, base_url: str, index: int) -> dict[str, Any] | None:
    """Extract data from a single section element."""
    # Determine section type
    tag_name = element.name
    classes = " ".join(element.get("class", []))
    section_id = element.get("id", "")

    # Classify section
    sec_type = _classify_section(tag_name, classes, section_id, element)

    # Extract inline styles
    inline_style = element.get("style", "")
    styles = _parse_inline_style(inline_style)

    # Extract background image from style
    bg_image = ""
    if "background-image" in inline_style or "background" in inline_style:
        bg_match = re.search(r'url\(["\']?([^"\')\s]+)', inline_style)
        if bg_match:
            bg_image = urljoin(base_url, bg_match.group(1))

    # Extract text content (first few paragraphs)
    texts: list[str] = []
    for p in element.find_all(["h1", "h2", "h3", "h4", "p", "span"], limit=10):
        text = p.get_text(strip=True)
        if text and len(text) > 2:
            texts.append(text[:200])

    # Extract images
    images = []
    for img in element.find_all("img", limit=5):
        src = img.get("src", "") or img.get("data-src", "")
        if src:
            images.append({"src": urljoin(base_url, src), "alt": img.get("alt", "")})

    # Extract links/buttons
    buttons = []
    for btn in element.find_all(["a", "button"], limit=5):
        btn_text = btn.get_text(strip=True)
        if btn_text:
            href = btn.get("href", "")
            btn_classes = " ".join(btn.get("class", []))
            is_button = btn.name == "button" or "btn" in btn_classes or "button" in btn_classes
            buttons.append({"text": btn_text, "href": href, "isButton": is_button})

    # Extract child component structure
    children = _extract_child_structure(element, depth=0, max_depth=3)

    if not texts and not images and not buttons and not children:
        return None

    return {
        "index": index,
        "type": sec_type,
        "tag": tag_name,
        "id": section_id,
        "classes": classes[:100],
        "styles": styles,
        "backgroundImage": bg_image,
        "texts": texts[:10],
        "images": images,
        "buttons": buttons,
        "children": children,
    }


def _classify_section(tag: str, classes: str, section_id: str, element: Tag) -> str:
    """Classify a section based on its tag, classes, and content."""
    combined = f"{tag} {classes} {section_id}".lower()

    if tag == "header" or "header" in combined or "navbar" in combined or "nav" in combined:
        return "navbar"
    if tag == "footer" or "footer" in combined:
        return "footer"
    if "hero" in combined or "banner" in combined or "jumbotron" in combined:
        return "hero"
    if "contact" in combined or "form" in combined:
        if element.find("form") or element.find("input"):
            return "contact-form"
    if "testimonial" in combined or "review" in combined:
        return "testimonials"
    if "feature" in combined or "service" in combined:
        return "features"
    if "about" in combined:
        return "about"
    if "gallery" in combined or "portfolio" in combined:
        return "gallery"
    if "pricing" in combined or "plan" in combined:
        return "pricing"
    if "faq" in combined:
        return "faq"
    if "cta" in combined or "call-to-action" in combined:
        return "cta"

    # Heuristic: if it has a large background image, it's probably a hero/banner
    style = element.get("style", "")
    if "background-image" in style or "background:" in style:
        h1 = element.find("h1")
        if h1:
            return "hero"

    return "section"


def _parse_inline_style(style: str) -> dict[str, str]:
    """Parse inline CSS style string into a dict."""
    if not style:
        return {}
    result: dict[str, str] = {}
    for declaration in style.split(";"):
        declaration = declaration.strip()
        if ":" in declaration:
            prop, _, value = declaration.partition(":")
            prop = prop.strip()
            value = value.strip()
            if prop and value:
                result[prop] = value
    return result


def _extract_child_structure(element: Tag, depth: int, max_depth: int) -> list[dict[str, Any]]:
    """Extract a simplified child component tree."""
    if depth >= max_depth:
        return []

    children: list[dict[str, Any]] = []
    for child in element.children:
        if not isinstance(child, Tag):
            continue
        if child.name in ("script", "style", "noscript", "link", "meta"):
            continue

        child_classes = " ".join(child.get("class", []))
        text = child.get_text(strip=True)[:100] if child.string else ""
        inline = child.get("style", "")

        entry: dict[str, Any] = {
            "tag": child.name,
            "classes": child_classes[:80],
        }
        if text and len(text) > 2:
            entry["text"] = text
        if inline:
            entry["styles"] = _parse_inline_style(inline)

        # Check for image
        if child.name == "img":
            entry["src"] = child.get("src", "") or child.get("data-src", "")
            entry["alt"] = child.get("alt", "")

        sub_children = _extract_child_structure(child, depth + 1, max_depth)
        if sub_children:
            entry["children"] = sub_children

        children.append(entry)

    return children[:20]  # Cap per level


def _extract_images(soup: BeautifulSoup, base_url: str) -> list[dict[str, str]]:
    """Extract all images from the page."""
    images: list[dict[str, str]] = []
    seen: set[str] = set()
    for img in soup.find_all("img", limit=20):
        src = img.get("src", "") or img.get("data-src", "")
        if src and src not in seen:
            seen.add(src)
            images.append({
                "src": urljoin(base_url, src),
                "alt": img.get("alt", ""),
            })
    return images


# ── Screenshot ───────────────────────────────────────────────────


async def _take_screenshot(url: str) -> str | None:
    """Take a viewport screenshot using Playwright. Returns base64 PNG or None."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.debug("Playwright not available for screenshots")
        return None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1440, "height": 900})
            await page.goto(url, wait_until="networkidle", timeout=20000)
            # Wait a bit for animations/lazy loading
            await asyncio.sleep(1)
            screenshot_bytes = await page.screenshot(full_page=False)
            await browser.close()
            return base64.b64encode(screenshot_bytes).decode("ascii")
    except Exception as e:
        logger.warning("Screenshot failed for %s: %s", url, e)
        return None


# ── Format for Agent ─────────────────────────────────────────────


def format_scraped_data_for_agent(data: dict[str, Any]) -> str:
    """Format scraped website data as concise text for the agent's context.

    Focuses on what the agent needs to recreate the site:
    - Section structure and types
    - Color palette
    - Fonts
    - Key content (headings, CTAs)
    - Navigation structure
    """
    if data.get("error"):
        return f"Failed to scrape {data['url']}: {data['error']}"

    parts: list[str] = []
    parts.append(f"## Scraped Website: {data['url']}")
    parts.append(f"Title: {data.get('title', '(none)')}")

    meta = data.get("meta", {})
    if meta.get("description"):
        parts.append(f"Description: {meta['description'][:200]}")

    # Colors
    colors = data.get("colors", [])
    if colors:
        parts.append(f"\nColor palette: {', '.join(colors[:10])}")

    # Fonts
    fonts = data.get("fonts", [])
    if fonts:
        parts.append(f"Fonts: {', '.join(fonts)}")

    # Navigation
    nav = data.get("navigation", [])
    if nav:
        nav_items = [f"{n['text']}" for n in nav]
        parts.append(f"\nNavigation: {' | '.join(nav_items)}")

    # Sections
    sections = data.get("sections", [])
    if sections:
        parts.append(f"\n## Page Structure ({len(sections)} sections)")
        for sec in sections:
            sec_type = sec.get("type", "section")
            parts.append(f"\n### Section: {sec_type}")

            styles = sec.get("styles", {})
            if styles:
                style_str = "; ".join(f"{k}: {v}" for k, v in list(styles.items())[:5])
                parts.append(f"  Styles: {style_str}")

            bg = sec.get("backgroundImage")
            if bg:
                parts.append(f"  Background image: {bg}")

            texts = sec.get("texts", [])
            if texts:
                for t in texts[:5]:
                    parts.append(f"  Text: \"{t}\"")

            images = sec.get("images", [])
            if images:
                for img in images[:3]:
                    parts.append(f"  Image: {img['src']} (alt: {img.get('alt', '')})")

            buttons = sec.get("buttons", [])
            if buttons:
                for btn in buttons[:3]:
                    btn_type = "Button" if btn.get("isButton") else "Link"
                    parts.append(f"  {btn_type}: \"{btn['text']}\" → {btn.get('href', '')}")

    # Images summary
    all_images = data.get("images", [])
    if all_images:
        parts.append(f"\n## Images ({len(all_images)} total)")
        for img in all_images[:8]:
            parts.append(f"  - {img['src']} (alt: {img.get('alt', '')})")

    return "\n".join(parts)


# ── URL Detection ────────────────────────────────────────────────


_URL_PATTERN = re.compile(
    r'https?://[^\s<>"\')\]]+',
    re.IGNORECASE,
)


def extract_urls_from_text(text: str) -> list[str]:
    """Extract HTTP/HTTPS URLs from user text."""
    return _URL_PATTERN.findall(text)
