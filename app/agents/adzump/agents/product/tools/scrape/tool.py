"""Scrape tool — Playwright-based page scraping for the ProductAgent.

Tool definition + orchestrator. The heavy concerns split out:
  - scrape_profile.py — gpt-4o summary, craft panel layout, summary input
  - scrape_assets.py  — logo/creative selection, upload, receipts row

Closures `paint_initial_screenshot` and `start_parallel_summary` stay here so they can
mutate the shared `state` dict on `_scrape_url`'s frame.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from functools import partial
from hashlib import md5
from urllib.parse import urlparse

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.agents.product.adapters.playwright_adapter import scrape_page
from app.agents.adzump.agents.product.scrape_stages import ScrapeStage, stage_emit
from app.agents.adzump._shared import clean_input_url, host_of, short_url, upload_screenshot
from .profile import (
    _format_page_for_profile,
    _generate_business_profile,
    _get_primary_profile,
)
from .assets import (
    _update_assets_from_extra_page,
    _select_and_persist_primary_assets,
)

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
    """Scrape a URL and return trimmed content + uploaded screenshot URL.

    Phases:
        1. Normalize url + resolve session state
        2. Reject duplicate urls + over the 5-scrape cap
        3. Init scrape-local state
        4. Streaming callbacks (closures that mutate shared `state` dict)
        5. Run scrape + handle failure
        6. Build result data + accumulate site links
        7. Post-scroll screenshot + non-primary craft section
        8. Primary vs non-primary post-processing
    """
    # ── 1. Validate input + resolve session state ───────────────────
    url = clean_input_url(params.get("url"))
    if not url:
        return ToolResult(success=False, error="url is required.")

    # emit_progress handles tool_update under the hood; stream is still
    # needed directly for emit_craft / emit_craft_text (live JSON panel).
    stream = context.get("event_stream")

    session_ctx = context.get("session_context") or {}
    product_data = session_ctx.setdefault("product_data", {})
    primary_url = product_data.get("primary_url")
    if not primary_url:
        product_data["primary_url"] = url
        primary_url = url

    # ── 2. Reject duplicate urls + over the 5-scrape cap ────────────
    # Prompt-level caps are unreliable with gpt-4o-mini — enforce in code.
    scraped_urls: list[str] = product_data.setdefault("scraped_urls", [])
    scrape_count = len(scraped_urls)
    if (reject := _reject_if_duplicate_or_over_cap(url, scraped_urls, scrape_count)):
        return reject

    # ── 3. Init scrape-local state ──────────────────────────────────
    # url == primary_url already implies same registrable host.
    is_primary = (url == primary_url)
    is_primary_scrape = (scrape_count == 0)
    craft_id = session_ctx.get("craft_id", "")
    scrape_id = md5(url.encode()).hexdigest()[:8]
    # Thread scrape_id so every stage_emit downstream tags its log line.
    context["scrape_id"] = scrape_id
    # Filename example: apple.com_a3b8f1c2.jpg — host makes it human-readable
    screenshot_filename = f"{host_of(url)}_{scrape_id}.jpg".lstrip("_")
    # State shared with the streaming callbacks below. Plain dict (BashTool-style
    # mutable captures) instead of `nonlocal` rewrites — fewer syntactic gymnastics,
    # same lifetime: locals on `_scrape_url`'s frame.
    state: dict = {"screenshot_url": None, "profile_task": None}

    await stage_emit(context, ScrapeStage.START, url=short_url(url))

    # ── 4. Streaming callbacks (mutate `state` in place) ────────────
    async def paint_initial_screenshot(b64: str) -> None:
        if not b64 or not stream:
            return
        try:
            uploaded = await upload_screenshot(
                base64.b64decode(b64), screenshot_filename, context,
            )
        except Exception as e:
            logger.debug("screenshot_upload_failed: %s", str(e)[:120])
            return
        if not uploaded:
            return
        state["screenshot_url"] = uploaded
        if is_primary and "primary_screenshot_url" not in product_data:
            product_data["primary_screenshot_url"] = uploaded
        if is_primary and craft_id:
            try:
                await stream.emit_craft(craft_id, url, [
                    {"id": "panel_image", "type": "image", "url": uploaded},
                    {"type": "callout", "text": "Analyzing…", "variant": "info"},
                ])
            except Exception as e:
                logger.debug("screenshot_craft_emit_failed: %s", str(e)[:120])

    async def start_parallel_summary(early_page) -> None:
        """Kick off summary in parallel with the slow scroll. Skips if not
        primary or if early HTML is thin (SPA fallback — summary then uses
        the post-scroll Page)."""
        if not is_primary_scrape or state["profile_task"] is not None:
            return
        if not _has_enough_text_for_summary(early_page):
            logger.info(
                "stage=summary_input scrape_id=%s source=early outcome=insufficient "
                "headings=%d paragraphs=%d → fallback to post-scroll",
                scrape_id, len(early_page.headings), len(early_page.paragraphs),
            )
            return
        logger.info(
            "stage=summary_input scrape_id=%s source=early headings=%d paragraphs=%d",
            scrape_id, len(early_page.headings), len(early_page.paragraphs),
        )
        # v4 (2026-05-25, I-1): SUMMARIZE attributes to SummaryAgent's own
        # tool_use_id, not the parent scrape tool's. See asset-picker-fixes-v4.
        import uuid as _uuid
        summary_tuid = _uuid.uuid4().hex[:12]
        # v6 S2 (2026-05-27): pre-emit agent_started BEFORE the first stage_emit
        # so the SUMMARIZE tool_update has an open span to route to. Otherwise
        # the UI sees a tool_update for an unknown tuid → drop. Sub-agent runs
        # with skip_started_emit=True to avoid double-emit. See
        # plans/agent-tracing/asset-picker-fixes-v6.html.
        if stream is not None:
            try:
                from app.core.streaming import current_agent_id as _curr_agent_id
                await stream.emit_agent_started(
                    agent_id="summary_gen",
                    label="Profile Writer",
                    parent_id=_curr_agent_id.get(),
                    parent_tool_use_id=context.get("tool_use_id", ""),
                    agent_tool_use_id=summary_tuid,
                )
                context.setdefault("_started_tuids", set()).add(summary_tuid)
            except Exception:
                logger.exception("v6_pre_emit_agent_started_failed agent=summary_gen")
        await stage_emit(context, ScrapeStage.SUMMARIZE, tool_use_id=summary_tuid)
        scraped_text = _format_page_for_profile(early_page)
        state["profile_task"] = asyncio.create_task(
            _generate_business_profile(
                scraped_text, url, stream, state["screenshot_url"], craft_id,
                auth=context.get("auth"),
                tool_use_id=context.get("tool_use_id", ""),
                parent_session_context=context.get("session_context"),
                agent_tool_use_id=summary_tuid,
                skip_started_emit=True,
            ),
            name=f"summary_{scrape_id}",
        )

    # ── 5. Run scrape + handle failure ──────────────────────────────
    result = await scrape_page(
        url,
        on_progress=partial(stage_emit, context),
        on_early_screenshot=paint_initial_screenshot,
        on_early_html=start_parallel_summary if is_primary_scrape else None,
    )
    if not result.success or not result.content:
        if state["profile_task"] is not None and not state["profile_task"].done():
            state["profile_task"].cancel()
        return ToolResult(
            success=False,
            error=result.error or f"Failed to scrape {url}",
            summary=f"Scrape failed for {url}: {result.error or 'unknown error'}",
        )

    # ── 6. Build result data + accumulate site links ────────────────
    scraped_urls.append(url)
    page = result.content
    data: dict = {
        "url": url,
        "title": page.title,
        "meta_description": page.meta_description,
        "headings": page.headings[:MAX_HEADINGS],
        "paragraphs": _trim_paragraphs(page.paragraphs),
        "links": [
            {"text": link.text, "href": link.href}
            for link in page.links[:MAX_LINKS]
        ],
    }
    _save_new_site_links(product_data, data["links"])

    # Shift 2 (2026-05-21): stash the full-page screenshot bytes in context so
    # the asset picker can prepend them as image block #0 in its vision call.
    # The adapter has already downsampled to ≤ 2000 px long-edge.
    if result.screenshot:
        context["full_page_screenshot_b64"] = result.screenshot

    # ── 7. Post-scroll screenshot + non-primary craft section ───────
    # If the summary task is running in parallel it already painted the
    # panel with the provisional URL; id-based replace swaps to the
    # final URL without disturbing the streaming summary text.
    if result.screenshot and stream:
        try:
            uploaded = await upload_screenshot(
                base64.b64decode(result.screenshot), screenshot_filename, context,
            )
            if uploaded:
                state["screenshot_url"] = uploaded
                data["screenshot_url"] = uploaded
                if is_primary:
                    product_data["primary_screenshot_url"] = uploaded
                if is_primary and craft_id and state["profile_task"] is not None:
                    try:
                        await stream.emit_craft(craft_id, url, [
                            {"id": "panel_image", "type": "image", "url": uploaded},
                        ], append=True)
                    except Exception as e:
                        logger.debug("image_swap_failed: %s", str(e)[:120])
        except Exception as e:
            logger.warning("final_screenshot_upload_failed: url=%s err=%s", url, str(e)[:200])

    # Non-primary scrapes append now that we have page.title.
    if stream and state["screenshot_url"] and craft_id and not is_primary:
        try:
            await stream.emit_craft(craft_id, url, [
                {"type": "divider"},
                {"type": "heading", "text": page.title or url},
                {"type": "image", "url": state["screenshot_url"]},
            ], append=True)
        except Exception as e:
            logger.debug("craft_emit_failed: %s", str(e)[:120])

    # ── 8. Branch: primary vs non-primary post-processing ───────────
    same_host = _is_same_website(url, primary_url)
    new_count = len(scraped_urls)
    remaining = MAX_SCRAPE_CALLS - new_count

    summary_lines = [
        f"Scraped {url}",
        f"Title: {page.title}",
    ]
    logger.info("stage=scrape scrape_id=%s headings=%d paragraphs=%d words≈%d budget=%d/%d",
                scrape_id, len(page.headings), len(page.paragraphs),
                sum(len(p.split()) for p in page.paragraphs),
                new_count, MAX_SCRAPE_CALLS)

    if is_primary_scrape:
        business_profile = await _get_primary_profile(
            state["profile_task"], page, url, stream, state["screenshot_url"], craft_id, context, scrape_id,
        )

        # Mutate the existing product_profile dict — parent chat-agent shares
        # it by reference (see agents/product/agent.py:306-311). Reassignment
        # detaches the sub-agent's copy and the summary never reaches the
        # parent for the storage save.
        profile = session_ctx.setdefault("product_profile", {})
        profile["summary"] = business_profile
        profile["title"] = page.title or ""
        profile["url"] = url

        if business_profile:
            summary_lines.append(f"\n## Product Summary\n{business_profile}")

        if same_host:
            await _select_and_persist_primary_assets(
                page, business_profile, product_data, context, stream, craft_id, url,
            )
        else:
            logger.info(
                "product_assets_skip: url=%s reason=different_host primary=%s",
                url, primary_url,
            )
    else:
        if same_host:
            await _update_assets_from_extra_page(page, product_data, context, url)
        if remaining == 0:
            summary_lines.append(
                "NEXT STEP: Budget exhausted. Write the FINAL JSON analysis now — "
                "do not scrape again."
            )
        elif remaining <= 2:
            summary_lines.append(
                "NEXT STEP: Low budget. If you haven't yet, run web_search for competitors, "
                "then scrape at most 1–2 competitor homepages and write the final JSON."
            )

    return ToolResult(success=True, data=data, summary="\n".join(summary_lines))


def _has_enough_text_for_summary(page) -> bool:
    """Heuristic: is the DOM-ready HTML rich enough to ground a summary, or
    is this an SPA that hydrates content post-load?"""
    if len(page.headings) < 2:
        return False
    return sum(len(p) for p in page.paragraphs) >= 200


def _is_same_website(a: str, b: str) -> bool:
    """Rough check: both URLs share the same registered domain suffix."""
    try:
        # removeprefix, not lstrip — lstrip("www.") treats "www." as a char SET
        # {w, .} and would mangle real domains like "wisco.com" → "isco.com".
        ha = urlparse(a).netloc.lower().removeprefix("www.")
        hb = urlparse(b).netloc.lower().removeprefix("www.")
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


def _reject_if_duplicate_or_over_cap(
    url: str, scraped_urls: list[str], scrape_count: int,
) -> ToolResult | None:
    """Reject re-scrapes and over-budget calls. Returns the error result to
    return to the agent, or None to proceed."""
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
    return None


def _save_new_site_links(product_data: dict, page_links: list[dict]) -> None:
    """Append page_links into product_data['site_links'], dedup by href.
    Surfaces under `siteLinks` in the parent agent's storage write."""
    site_links: list[dict] = product_data.setdefault("site_links", [])
    seen_hrefs = {l.get("href") for l in site_links if l.get("href")}
    for link in page_links:
        if link["href"] not in seen_hrefs:
            site_links.append(link)
            seen_hrefs.add(link["href"])
