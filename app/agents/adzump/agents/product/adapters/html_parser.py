"""Shared HTML parsing utility.

Extracts structured content from raw HTML.
Captures all visible text, not just <p> tags.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag, NavigableString

from app.agents.adzump.agents.product.models import PageContent, SiteLink, SiteImage

logger = logging.getLogger(__name__)

# Tags to skip when extracting visible text
_SKIP_TAGS = {"script", "style", "noscript", "svg", "path", "meta", "link", "head"}

# Cap the per-page image candidate list. Above this is almost always icons,
# tracking pixels, payment-method logos — and the LLM selector sees a smaller
# prompt with the same useful signal.
MAX_IMAGES = 50

# Drop tiny images (icons, tracking pixels, decoration) when dims are declared.
_MIN_IMAGE_DIM = 32


def parse_html(
    url: str,
    html: str,
    network_images: list[dict] | None = None,
    image_positions: dict[str, dict] | None = None,
) -> PageContent:
    """Parse HTML into structured PageContent. No selection logic — this is
    pure evidence gathering for the LLM selector in product_assets.py.

    `network_images` is an optional list of `{url, content_type, size}` dicts
    captured at the network layer by the adapter. These get merged into the
    candidate list as `source="network"`. The DOM declares what the page
    asserts about itself; network responses are what the browser actually
    loaded. SPAs frequently render images via CSS background / canvas /
    inline-JSON-driven layouts; those only surface here.

    `image_positions` is a map of `src → {top, left, width, height}` from a
    Playwright bounding-box capture. The parser annotates each SiteImage with
    its rendered geometry so downstream code can recognize header logos by
    visual position on framework sites that lack semantic `<header>` tags."""
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
        p.get_text(strip=True)
        for p in soup.find_all("p")
        if p.get_text(strip=True)
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

    structured_data = _extract_json_ld(soup)
    images = _collect_image_candidates(url, soup, structured_data)
    if network_images:
        images = _merge_network_images(images, network_images)
    if image_positions:
        for img in images:
            pos = image_positions.get(img.src)
            if not pos:
                continue
            img.rendered_top = pos.get("top")
            img.rendered_left = pos.get("left")
            img.rendered_width = pos.get("width")
            img.rendered_height = pos.get("height")

    return PageContent(
        url=url,
        title=title,
        meta_description=meta_description,
        headings=headings,
        paragraphs=paragraphs,
        links=links,
        images=images,
        structured_data=structured_data,
    )


# ─── Image candidate gathering ──────────────────────────────────────────


def _collect_image_candidates(
    page_url: str, soup: BeautifulSoup, structured_data: dict | None
) -> list[SiteImage]:
    """Collect image candidates from every plausible source. No selection.

    Pages routinely repeat the same image URL across many tags (the same
    logo rendered in <header>, hero overlay, and <footer>). We dedupe by
    `src` so the candidate list reflects unique images, and only stop
    iterating when the per-page cap (MAX_IMAGES) is hit.

    Sources, in order of insertion:
      - <img> tags (with ancestor + class/id context)
      - <picture><source srcset> (largest)
      - <meta property="og:image" / og:logo / twitter:image>
      - <link rel="icon" / "apple-touch-icon" / "mask-icon"> with sizes
      - JSON-LD Organization.logo, Product.image
    """
    images: list[SiteImage] = []
    seen_srcs: set[str] = set()

    def add(img: SiteImage) -> None:
        """Append `img` to the candidate list unless its src is already
        present. Silent dedup — caller checks `len(images) >= MAX_IMAGES`
        separately to decide when to stop iterating.

        v9 (2026-05-22): SVG candidates dropped at the parser. The picker
        can't use SVGs as ad creatives (need raster), and the safety-net
        that historically used SVG filename + dimension heuristics was
        retired. Parser yields raster-only candidates by contract.
        """
        if not img or img.src in seen_srcs:
            return
        if _is_svg_src(img.src):
            return
        seen_srcs.add(img.src)
        images.append(img)

    for tag in soup.find_all(["img", "picture", "meta", "link"]):
        if not isinstance(tag, Tag):
            continue
        if len(images) >= MAX_IMAGES:
            break

        cand: SiteImage | None = None
        if tag.name == "img":
            cand = _candidate_from_img(page_url, tag)
        elif tag.name == "picture":
            cand = _candidate_from_picture(page_url, tag)
        elif tag.name == "meta":
            cand = _candidate_from_meta(page_url, tag)
        elif tag.name == "link":
            cand = _candidate_from_link(page_url, tag)
        if cand:
            add(cand)

    if len(images) < MAX_IMAGES:
        for cand in _candidates_from_jsonld(page_url, structured_data):
            if len(images) >= MAX_IMAGES:
                break
            add(cand)

    return images


def _merge_network_images(
    candidates: list[SiteImage], network_images: list[dict]
) -> list[SiteImage]:
    """Append network-captured image URLs not already in the candidate list.

    DOM candidates carry richer metadata (alt, in_header, class). When a
    network URL matches an existing DOM candidate's src we keep the DOM
    entry — same image, better signal. Anything new gets a `source="network"`
    entry with bare metadata; the LLM still has the URL + the screenshot to
    reason about it."""
    seen = {img.src for img in candidates}
    out = list(candidates)
    for ni in network_images:
        n_url = (ni or {}).get("url") or ""
        if not n_url or n_url in seen:
            continue
        if _is_svg_src(n_url):
            continue
        out.append(SiteImage(src=n_url, source="network"))
        seen.add(n_url)
        if len(out) >= MAX_IMAGES:
            break
    return out


def _is_svg_src(src: str) -> bool:
    """v9: parser-level SVG filter. Catches both `.svg` extension and the
    rare `image/svg+xml` data URI. Query strings stripped before extension
    check (`logo.svg?v=2` still matches)."""
    if not src:
        return False
    lower = src.lower()
    if lower.startswith("data:image/svg"):
        return True
    return lower.split("?", 1)[0].endswith(".svg")


def _candidate_from_img(page_url: str, img: Tag) -> SiteImage | None:
    raw_src = img.get("src") or img.get("data-src")
    if not isinstance(raw_src, str):
        return None
    raw_src = raw_src.strip()
    if not raw_src or raw_src.startswith("data:"):
        return None

    src = urljoin(page_url, raw_src)
    width = _parse_dim(img.get("width"))
    height = _parse_dim(img.get("height"))
    if width is not None and width < _MIN_IMAGE_DIM:
        return None
    if height is not None and height < _MIN_IMAGE_DIM:
        return None

    return SiteImage(
        src=src,
        alt=str(img.get("alt", "")).strip(),
        title=str(img.get("title", "")).strip(),
        width=width,
        height=height,
        source="img",
        in_header=img.find_parent("header") is not None,
        in_footer=img.find_parent("footer") is not None,
        in_nav=img.find_parent("nav") is not None,
        class_attr=" ".join(img.get("class") or []) if isinstance(img.get("class"), list) else str(img.get("class") or ""),
        id_attr=str(img.get("id") or ""),
    )


def _candidate_from_picture(page_url: str, picture: Tag) -> SiteImage | None:
    """Pick the largest URL from any <source srcset> inside <picture>.

    <picture> wraps <source>s and a fallback <img>. The <img> already gets its
    own candidate; this surfaces the larger source variants the parser would
    otherwise miss."""
    best_src = ""
    best_w = 0
    for source in picture.find_all("source"):
        if not isinstance(source, Tag):
            continue
        srcset = source.get("srcset")
        if not isinstance(srcset, str):
            continue
        for url, width in _parse_srcset(srcset):
            if width > best_w:
                best_src = url
                best_w = width

    if not best_src:
        return None

    src = urljoin(page_url, best_src)
    return SiteImage(
        src=src,
        source="picture",
        width=best_w or None,
        in_header=picture.find_parent("header") is not None,
        in_footer=picture.find_parent("footer") is not None,
        in_nav=picture.find_parent("nav") is not None,
    )


_OG_LOGO_PROPS = {"og:image", "og:logo", "twitter:image", "twitter:image:src"}


def _candidate_from_meta(page_url: str, meta: Tag) -> SiteImage | None:
    prop = meta.get("property") or meta.get("name")
    if not isinstance(prop, str) or prop not in _OG_LOGO_PROPS:
        return None
    content = meta.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    return SiteImage(
        src=urljoin(page_url, content.strip()),
        source="og",
        title=prop,  # records which OG/Twitter property surfaced it
    )


_LINK_ICON_RELS = {"icon", "apple-touch-icon", "apple-touch-icon-precomposed", "mask-icon"}


def _candidate_from_link(page_url: str, link: Tag) -> SiteImage | None:
    rel = link.get("rel")
    rel_set = set(rel) if isinstance(rel, list) else {str(rel or "")}
    if not (rel_set & _LINK_ICON_RELS):
        return None
    href = link.get("href")
    if not isinstance(href, str) or not href.strip():
        return None
    # Parse "180x180" sizes attribute if present.
    sizes = str(link.get("sizes") or "")
    width = height = None
    if "x" in sizes:
        a, _, b = sizes.partition("x")
        try:
            width = int(a)
            height = int(b)
        except ValueError:
            pass
    return SiteImage(
        src=urljoin(page_url, href.strip()),
        source="link",
        title=" ".join(sorted(rel_set & _LINK_ICON_RELS)),
        width=width,
        height=height,
    )


def _candidates_from_jsonld(page_url: str, data: dict | None) -> list[SiteImage]:
    """Walk JSON-LD looking for Organization.logo / Product.image / image fields."""
    out: list[SiteImage] = []
    if not data:
        return out

    seen: set[str] = set()

    def visit(node):
        if isinstance(node, list):
            for child in node:
                visit(child)
            return
        if not isinstance(node, dict):
            return
        # @graph is the standard nesting; recurse.
        if "@graph" in node:
            visit(node["@graph"])
        node_type = node.get("@type")
        type_str = ",".join(node_type) if isinstance(node_type, list) else str(node_type or "")

        for key in ("logo", "image"):
            value = node.get(key)
            for url in _flatten_url(value):
                if url and url not in seen:
                    seen.add(url)
                    out.append(SiteImage(
                        src=urljoin(page_url, url),
                        source="jsonld",
                        title=f"{type_str}.{key}" if type_str else key,
                    ))
        # Recurse into nested dicts that may contain Organization / Product etc.
        for v in node.values():
            if isinstance(v, (dict, list)):
                visit(v)

    visit(data)
    return out


def _flatten_url(value) -> list[str]:
    """JSON-LD image/logo can be a string, a dict with `url`, or a list of either."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        url = value.get("url") or value.get("@id")
        return [url.strip()] if isinstance(url, str) and url.strip() else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_flatten_url(item))
        return out
    return []


def _parse_srcset(srcset: str) -> list[tuple[str, int]]:
    """Parse a srcset attribute into (url, width) pairs. Width 0 if descriptor missing."""
    out: list[tuple[str, int]] = []
    for part in srcset.split(","):
        part = part.strip()
        if not part:
            continue
        url, _, desc = part.partition(" ")
        url = url.strip()
        desc = desc.strip().rstrip("w")
        try:
            width = int(desc) if desc else 0
        except ValueError:
            width = 0
        out.append((url, width))
    return out


def _parse_dim(value) -> int | None:
    """Parse an HTML width/height attribute. Tolerates '100', '100px', '100%'."""
    if not value:
        return None
    s = str(value).strip()
    digits = ""
    for ch in s:
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else None


# ─── Text extraction ────────────────────────────────────────────────────


def _extract_visible_text(soup: BeautifulSoup) -> list[str]:
    """Extract meaningful visible text blocks from the page body."""
    body = soup.find("body")
    if not body:
        return []

    texts: list[str] = []
    seen: set[str] = set()

    for element in body.find_all(["div", "section", "article", "li", "td", "blockquote"]):
        if element.name in _SKIP_TAGS:
            continue

        # Get direct text (not from child block elements)
        direct_text = ""
        for child in element.children:
            if isinstance(child, NavigableString):
                direct_text += child.strip() + " "
            elif isinstance(child, Tag) and child.name in ("span", "strong", "em", "b", "i", "a", "br"):
                direct_text += child.get_text(strip=True) + " "

        direct_text = direct_text.strip()
        if direct_text and len(direct_text) > 20 and direct_text not in seen:
            seen.add(direct_text)
            texts.append(direct_text)

    return texts


# ─── JSON-LD ────────────────────────────────────────────────────────────


def _extract_json_ld(soup: BeautifulSoup) -> dict | None:
    """Iterate ALL <script type="application/ld+json"> blocks. Returns a merged
    dict (under "@graph" when multiple blocks exist) so consumers can walk a
    single shape regardless of how many JSON-LD scripts a page ships."""
    blocks: list = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not isinstance(script, Tag) or not script.string:
            continue
        try:
            data = json.loads(script.string)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, list):
            blocks.extend(data)
        else:
            blocks.append(data)

    if not blocks:
        return None
    if len(blocks) == 1 and isinstance(blocks[0], dict):
        return blocks[0]
    return {"@graph": blocks}
