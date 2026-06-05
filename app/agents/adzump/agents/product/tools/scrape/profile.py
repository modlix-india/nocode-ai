"""Product profile generation — gpt-4o summary + craft-panel layout.

The craft panel layout is emitted from here (in `_generate_business_profile`)
with stable block ids: `panel_image`, `assets_label`, `assets_row`,
`assets_divider`, `summary_heading`, `summary_text`. The asset-receipt
updates in `scrape_assets.py` replace `assets_label` / `assets_row` in
place via the UI's id-based merge, so the layout emit MUST run before
the first receipt emit. Today the ordering is guaranteed by call sequence
in `_scrape_url` (scrape.py).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.agents.adzump.agents.product.scrape_stages import ScrapeStage, stage_emit

logger = logging.getLogger(__name__)

# System prompt lives in agents/product/prompts/ alongside product_assets.txt.
# Read once at module load.
_PROFILE_PROMPT = (
    Path(__file__).resolve().parent.parent.parent
    / "prompts" / "product_profile.txt"
).read_text(encoding="utf-8")


def _format_page_for_profile(page) -> str:
    return "\n".join([
        f"Title: {page.title or ''}",
        f"Meta: {page.meta_description or ''}",
        f"Headings: {'; '.join(page.headings[:50])}",
        "\n".join(p[:400] for p in page.paragraphs[:15]),
    ])


async def _generate_business_profile(
    scraped_text: str,
    url: str,
    stream,
    screenshot_url: str | None,
    craft_id: str,
    auth=None,
    tool_use_id: str = "",
    parent_session_context: dict | None = None,
    agent_tool_use_id: str = "",
    skip_started_emit: bool = False,
) -> str:
    """Generate a product profile from scraped content via SummaryAgent.

    Emits the panel's FINAL layout (image, asset receipt placeholders,
    divider, heading, summary text) before the LLM stream starts — receipt
    blocks fill in later via id-based replace from `_emit_asset_receipts`.

    Streaming + cancellation now live inside `SummaryAgent.summarize()`
    via `_CraftBoundStream` routing assistant text deltas to the
    `summary_text` craft block.
    """
    if stream:
        try:
            image_block: dict = {"id": "panel_image", "type": "image"}
            if screenshot_url:
                image_block["url"] = screenshot_url
            await stream.emit_craft(craft_id, url, [
                image_block,
                {"id": "assets_label", "type": "text", "content": ""},
                {"id": "assets_row", "type": "row", "children": []},
                {"id": "assets_divider", "type": "divider"},
                {"id": "summary_heading", "type": "heading", "text": "Product Summary"},
                {"id": "summary_text", "type": "text", "content": ""},
            ], append=False)
        except Exception:
            pass

    if auth is None:
        # Defensive: a caller missing the auth hand-off would surface here.
        # Returns empty so the layout placeholder stays visible but no
        # summary text streams in. Caller logs the missing auth.
        logger.warning("summary_skip_no_auth url=%s", url)
        return ""

    try:
        from app.agents.adzump.agents.summary import get_summary_agent
        result = await get_summary_agent().summarize(
            scraped_text=scraped_text,
            url=url,
            parent_event_stream=stream,
            parent_tool_use_id=tool_use_id,
            auth=auth,
            craft_id=craft_id,
            parent_session_context=parent_session_context,
            # v5 (2026-05-25, I-1): bind Profile Writer's row to the
            # summary_tuid so SUMMARIZE stage_emit (already using this id
            # from v4) and the agent_started lifecycle event share an id —
            # UI can group correctly. See asset-picker-fixes-v5.
            agent_tool_use_id=agent_tool_use_id,
            # v6 S2 (2026-05-27): caller pre-emitted agent_started before
            # SUMMARIZE stage_emit so the UI had a span to route to. Tell
            # BaseAgent.run() not to double-emit.
            skip_started_emit=skip_started_emit,
        )
        return result.text
    except Exception as e:
        logger.warning("business_profile_failed: %s: %s",
                       type(e).__name__, str(e)[:200])
        return ""


async def _get_primary_profile(
    profile_task: asyncio.Task | None,
    page,
    url: str,
    stream,
    screenshot_url: str | None,
    craft_id: str,
    context: dict,
    scrape_id: str,
) -> str:
    """Return the primary-page business profile. Awaits the parallel task
    started in `start_parallel_summary` when DOM-ready HTML was rich enough;
    otherwise runs the fallback on the post-scroll Page.

    `screenshot_url` is passed by value — the parallel task already captured
    its own snapshot at create time. Do NOT change this to read from a shared
    state object; the post-scroll URL would leak into the provisional panel.
    """
    if profile_task is not None:
        try:
            return await profile_task
        except Exception as e:
            logger.warning("summary_task_failed scrape_id=%s err=%s",
                           scrape_id, str(e)[:200])
            return ""
    # v4 (2026-05-25, I-1): SUMMARIZE attributes to SummaryAgent's own
    # tool_use_id, not the parent scrape tool's. See asset-picker-fixes-v4.
    import uuid as _uuid
    summary_tuid = _uuid.uuid4().hex[:12]
    await stage_emit(context, ScrapeStage.SUMMARIZE, tool_use_id=summary_tuid)
    logger.info("stage=summary_input scrape_id=%s source=post_scroll", scrape_id)
    scraped_text = _format_page_for_profile(page)
    return await _generate_business_profile(
        scraped_text, url, stream, screenshot_url, craft_id,
        auth=context.get("auth"),
        tool_use_id=context.get("tool_use_id", ""),
        parent_session_context=context.get("session_context"),
        # v5: same UUID as the SUMMARIZE stage_emit above — so the UI
        # binds Profile Writer's row to that id and DISCOVER/SELECT/etc
        # from inside the summary don't collapse onto another row.
        agent_tool_use_id=summary_tuid,
    )
