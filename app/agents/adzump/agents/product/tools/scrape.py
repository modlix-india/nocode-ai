"""Scrape tool — Playwright-based page scraping for the ProductAgent.

Wraps the enhanced Playwright scraper, returns trimmed page content +
uploaded screenshot URL, emits craft-panel updates for live progress,
and generates a product profile on the primary scrape via gpt-4o.
"""

from __future__ import annotations

import base64
import logging
from hashlib import md5
from urllib.parse import urlparse

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.agents.product.adapters.playwright_adapter import scrape_page
from app.agents.adzump.tools._shared import emit_progress, upload_screenshot

logger = logging.getLogger(__name__)

# Trim limits for tool output to keep the agent's context small.
MAX_PARAGRAPHS = 50
MAX_HEADINGS = 50
MAX_LINKS = 50
MAX_PARAGRAPH_CHARS = 1000

# Hard budget enforced in code — prompt-level budgets are routinely ignored
# by gpt-4o-mini. After this many successful scrapes the tool refuses further
# calls and tells the model to move on (web_search or final JSON).
MAX_SCRAPE_CALLS = 5

scrape_url = ToolDefinition(
    name="scrape_url",
    description=(
        "Fetch a webpage and return its headings, paragraphs, links, meta description, "
        "and a screenshot URL. Only scrape URLs you have been explicitly told to scrape "
        "or that appear in search results. Do NOT guess sub-page URLs (like /about, "
        "/amenities) — they may not exist. Output is trimmed; headings + first 20 "
        "paragraphs are enough to reason about a page."
    ),
    display_name="Scrape URL",
    parameters=[
        ToolParameter(
            name="url",
            type="string",
            description="Absolute URL to scrape (https://…). Scrapes one URL per call. Call multiple times for additional pages.",
            required=True,
        ),
    ],
    execute=lambda params, context: _scrape_url(params, context),
)

async def _scrape_url(params: dict, context: dict) -> ToolResult:
    """Scrape a URL and return trimmed content + uploaded screenshot URL."""
    url = (params.get("url") or "").strip()
    if not url:
        return ToolResult(success=False, error="url is required.")
    if not url.startswith("http"):
        url = f"https://{url}"

    # emit_progress handles tool_update under the hood; stream is still
    # needed directly for emit_craft / emit_craft_text (live JSON panel).
    stream = context.get("event_stream")

    # Is this the primary business URL (first scrape) or a follow-up (competitor/subpage)?
    session_ctx = context.get("session_context") or {}
    product_data = session_ctx.setdefault("product_data", {})
    primary_url = product_data.get("primary_url")
    if not primary_url:
        product_data["primary_url"] = url
        primary_url = url

    # Enforce budget — prompt-level caps are unreliable with gpt-4o-mini.
    scrape_count = int(product_data.get("scrape_count", 0))
    scraped_urls: list[str] = product_data.setdefault("scraped_urls", [])
    if url in scraped_urls:
        return ToolResult(
            success=False,
            error=(
                f"Already scraped {url} earlier this session. "
                "Do NOT re-scrape the same URL. If you have enough information, "
                "call web_search for competitors or write the final JSON now."
            ),
        )
    if scrape_count >= MAX_SCRAPE_CALLS:
        return ToolResult(
            success=False,
            error=(
                f"Scrape budget exhausted ({scrape_count}/{MAX_SCRAPE_CALLS} pages scraped). "
                "Stop scraping. If you have not yet discovered competitors, call web_search "
                "ONCE. Otherwise, write the final JSON analysis NOW using the data you have."
            ),
        )

    from app.agents.adzump.tools.research import _short_url
    await emit_progress(context, f"Scraping {_short_url(url)}")

    result = await scrape_page(url)
    if not result.success or not result.content:
        return ToolResult(
            success=False,
            error=result.error or f"Failed to scrape {url}",
            summary=f"Scrape failed for {url}: {result.error or 'unknown error'}",
        )

    # Record the successful scrape against the budget.
    product_data["scrape_count"] = scrape_count + 1
    scraped_urls.append(url)

    page = result.content
    page_links = [
        {"text": link.text, "href": link.href}
        for link in page.links[:MAX_LINKS]
    ]

    data: dict = {
        "url": url,
        "title": page.title,
        "meta_description": page.meta_description,
        "headings": page.headings[:MAX_HEADINGS],
        "paragraphs": _trim_paragraphs(page.paragraphs),
        "links": page_links,
    }

    # Accumulate links into product_data so the storage write surfaces them
    # under `siteLinks`. Dedup by href (different pages link to same href).
    site_links: list[dict] = product_data.setdefault("site_links", [])
    seen_hrefs = {l.get("href") for l in site_links if l.get("href")}
    for link in page_links:
        if link["href"] not in seen_hrefs:
            site_links.append(link)
            seen_hrefs.add(link["href"])

    # Upload screenshot and emit craft updates.
    screenshot_url: str | None = None
    if result.screenshot and stream:
        try:
            screenshot_bytes = base64.b64decode(result.screenshot)
            filename = f"analysis_{md5(url.encode()).hexdigest()[:8]}.jpg"
            screenshot_url = await upload_screenshot(screenshot_bytes, filename, context)
            if screenshot_url:
                data["screenshot_url"] = screenshot_url
                # Stash the primary-page screenshot URL for the caller
                # (analyze_business tool) to rebuild the final craft panel.
                if url == primary_url and "primary_screenshot_url" not in product_data:
                    product_data["primary_screenshot_url"] = screenshot_url
        except Exception as e:
            logger.warning("analyst_screenshot_upload_failed: url=%s err=%s", url, str(e)[:200])

    # Emit craft panel: primary URL creates the panel, follow-ups append to it.
    craft_id = session_ctx.get("craft_id", "")
    if stream and screenshot_url and craft_id:
        is_primary = _same_registrable_host(url, primary_url) and url == primary_url
        try:
            if is_primary:
                await stream.emit_craft(craft_id, url, [
                    {"type": "image", "url": screenshot_url},
                    {"type": "callout", "text": "Analyzing…", "variant": "info"},
                ])
            else:
                await stream.emit_craft(craft_id, url, [
                    {"type": "divider"},
                    {"type": "heading", "text": page.title or url},
                    {"type": "image", "url": screenshot_url},
                ], append=True)
        except Exception as e:
            logger.debug("craft_emit_failed: %s", str(e)[:120])

    new_count = product_data["scrape_count"]
    remaining = MAX_SCRAPE_CALLS - new_count
    is_primary_scrape = (new_count == 1)

    summary_lines = [
        f"Scraped {url}",
        f"Title: {page.title}",
    ]
    logger.info("scrape_url: headings=%d paragraphs=%d words≈%d budget=%d/%d",
                len(page.headings), len(page.paragraphs),
                sum(len(p.split()) for p in page.paragraphs),
                new_count, MAX_SCRAPE_CALLS)

    # Directive next-step hint based on where we are in the budget.
    if is_primary_scrape:
        # Generate a business summary from the scraped content using a cheap
        # LLM call. This serves two purposes:
        # 1. Streams to the craft panel so user sees value immediately (~3s)
        # 2. Stored as the business brief so web_search uses rich context
        await emit_progress(context, "Generating product summary…")

        scraped_text = "\n".join([
            f"Title: {page.title or ''}",
            f"Meta: {page.meta_description or ''}",
            f"Headings: {'; '.join(page.headings[:MAX_HEADINGS])}",
            "\n".join(p[:400] for p in page.paragraphs[:15]),
        ])

        business_summary = await _generate_business_profile(
            scraped_text, url, stream, screenshot_url, craft_id
        )

        # Store the rich summary as the business brief for web_search context.
        # Mutate the existing dict instead of wholesale-reassigning — the
        # parent chat-agent shares this dict by reference (see
        # agents/product/agent.py:306-311). Reassignment would detach the
        # sub-agent's copy from the parent, and the rich summary would
        # never reach the parent for the storage save.
        session_ctx = context.get("session_context") or {}
        profile = session_ctx.setdefault("product_profile", {})
        profile["summary"] = business_summary
        profile["title"] = page.title or ""
        profile["url"] = url

        if business_summary:
            summary_lines.append(f"\n## Product Summary\n{business_summary}")
    elif remaining == 0:
        summary_lines.append(
            "NEXT STEP: Budget exhausted. Write the FINAL JSON analysis now — do not scrape again."
        )
    elif remaining <= 2:
        summary_lines.append(
            "NEXT STEP: Low budget. If you haven't yet, run web_search for competitors, "
            "then scrape at most 1–2 competitor homepages and write the final JSON."
        )

    return ToolResult(success=True, data=data, summary="\n".join(summary_lines))


# ── Helpers ───────────────────────────────────────────────────────────

_PROFILE_PROMPT = """You are a product analyst. Given scraped website content, write a concise product profile in 4-6 short paragraphs.

Paragraph 1: What is it — product name, format (e.g. "3 & 4 BHK duplex villaments"), location (road/area/city).
Paragraph 2: Variants and pricing — list each variant with its price point in one line each.
Paragraph 3: Target customer — who buys this and why.
Paragraph 4: Key differentiators — what makes it stand out (2-3 points, one sentence each).
Paragraph 5 (if applicable): Trust signals — awards, certifications, occupancy status.

RULES:
- Be specific and factual — use exact numbers, names, prices from the content.
- Keep it scannable — short sentences, no filler.
- ABSOLUTELY NO formatting: no bold, no asterisks, no markdown, no bullets, no dashes as list markers. Just plain sentences separated by line breaks.
- Only use information present in the content.
- Do NOT include URLs."""


async def _generate_business_profile(
    scraped_text: str,
    url: str,
    stream,
    screenshot_url: str | None,
    craft_id: str,
) -> str:
    """Generate a product profile from scraped content.

    Streams the profile to the craft panel so user sees value immediately.
    Returns the profile text.
    """
    from openai import AsyncOpenAI
    from app.config import settings

    # Set up craft panel with screenshot + empty text block for streaming.
    if stream and screenshot_url:
        try:
            await stream.emit_craft(craft_id, url, [
                {"type": "image", "url": screenshot_url},
                {"type": "heading", "text": "Product Summary"},
                {"type": "text", "content": ""},
            ], append=False)
        except Exception:
            pass

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    accumulated: list[str] = []

    try:
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": _PROFILE_PROMPT},
                {"role": "user", "content": f"Website: {url}\n\n{scraped_text[:15000]}"},
            ],
            temperature=0,
            max_tokens=3000,
            stream=True,
        )
        async for chunk in resp:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if not delta:
                continue
            accumulated.append(delta)

            if stream:
                try:
                    await stream.emit_craft_text(craft_id, delta)
                except Exception:
                    pass
    except Exception as e:
        logger.warning("business_profile_failed: %s: %s",
                       type(e).__name__, str(e)[:200])

    return "".join(accumulated).strip()


def _same_registrable_host(a: str, b: str) -> bool:
    """Rough check: both URLs share the same registered domain suffix."""
    try:
        ha = urlparse(a).netloc.lower().lstrip("www.")
        hb = urlparse(b).netloc.lower().lstrip("www.")
        return bool(ha) and bool(hb) and (ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha))
    except Exception:
        return False


def _trim_paragraphs(paragraphs: list[str]) -> list[str]:
    trimmed: list[str] = []
    for p in paragraphs[:MAX_PARAGRAPHS]:
        if len(p) > MAX_PARAGRAPH_CHARS:
            trimmed.append(p[:MAX_PARAGRAPH_CHARS].rstrip() + "…")
        else:
            trimmed.append(p)
    return trimmed
