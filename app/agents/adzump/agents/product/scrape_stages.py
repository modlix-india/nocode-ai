"""Scrape pipeline stage vocabulary for the Product Analyst.

Each Stage carries:
  - a stable id (used in logs)
  - a user-facing present-tense message template

Code references stages by enum value; user-visible text + structured log line
come from one place. Add new stages here, never inline a message string.

Lives under agents/product/ because it's coupled to the scrape tool's
pipeline. Other tools/pipelines should define their own stage modules.
"""

from __future__ import annotations

import logging
from enum import Enum

from app.agents.adzump._shared import emit_progress

logger = logging.getLogger(__name__)


class ScrapeStage(str, Enum):
    """Pipeline stages for the Product Analyst's scrape tool."""

    START      = "scrape.start"
    FETCH      = "scrape.fetch"
    READ       = "scrape.read"
    CAPTURE    = "scrape.capture"
    SCROLL     = "scrape.scroll"
    SUMMARIZE  = "scrape.summarize"
    DISCOVER   = "scrape.discover"
    SELECT     = "scrape.select"
    SAVE_LOGO  = "scrape.save_logo"
    SAVE_IMG   = "scrape.save_img"


_MESSAGES: dict[ScrapeStage, str] = {
    ScrapeStage.START:      "Scraping {url}",
    ScrapeStage.FETCH:      "Loading site…",
    ScrapeStage.READ:       "Reading page content…",
    ScrapeStage.CAPTURE:    "Capturing snapshot…",
    ScrapeStage.SCROLL:     "Scrolling for more images…",
    ScrapeStage.SUMMARIZE:  "Generating marketing summary…",
    ScrapeStage.DISCOVER:   "Analyzing {n} candidate images…",
    ScrapeStage.SELECT:     "Picking the best images…",
    ScrapeStage.SAVE_LOGO:  "Saving logo…",
    ScrapeStage.SAVE_IMG:   "Saving product image {i}/{n}…",
}


# Names emitted by generic adapters (e.g. playwright_adapter) → ScrapeStage.
# Keeps the adapter free of tool-specific vocabulary while letting the tool
# render user-facing text from one place.
ADAPTER_TO_SCRAPE: dict[str, ScrapeStage] = {
    "fetch":   ScrapeStage.FETCH,
    "read":    ScrapeStage.READ,
    "capture": ScrapeStage.CAPTURE,
    "scroll":  ScrapeStage.SCROLL,
}


async def stage_emit(
    context: dict, stage: ScrapeStage | str,
    tool_use_id: str | None = None,
    **kwargs,
) -> None:
    """Emit a tool_update for the user + a structured log line.

    Accepts either a ScrapeStage (tool-side callers) or a generic adapter
    name string like ``"fetch"`` (adapter callbacks) — strings are looked up
    via ADAPTER_TO_SCRAPE. Unknown names are silently ignored so a new
    adapter stage can ship before its mapping does.

    ``kwargs`` fill the message template (e.g. n=23 for DISCOVER). Missing
    placeholders fall back to their template tokens so we never crash on
    a formatting bug — visible weirdness > silent skipped events.

    ``tool_use_id`` overrides the context's tool_use_id when provided. Used
    by sub-agents (SummaryAgent · AssetPickerAgent) to attribute their own
    stage events to their own row instead of the parent scrape tool's row.
    See asset-picker-fixes-v4 I-1 + Kiran's panel-review mapping:
      · SUMMARIZE×2 → SummaryAgent's own tool_use_id
      · DISCOVER + SELECT → AssetPicker's own tool_use_id
      · SAVE_LOGO + SAVE_IMG → parent scrape's tool_use_id (no override)
    """
    if isinstance(stage, str) and not isinstance(stage, ScrapeStage):
        stage = ADAPTER_TO_SCRAPE.get(stage)
        if stage is None:
            return
    template = _MESSAGES.get(stage, str(stage.value))
    try:
        message = template.format(**kwargs)
    except (KeyError, IndexError):
        message = template
    # v6 S2 (2026-05-27 · Lance's panel-review ask): warn when a stage_emit
    # fires with a tool_use_id that hasn't been pre-emitted via
    # agent_started yet. Post-v6 this should never log. If it does, a new
    # spawn site is firing stage_emits without pre-emitting (regression).
    # The set lives on the context dict — pre_emit_agent_started (the shared
    # launcher helper) registers each tuid there. Per-scrape lifecycle, no globals.
    effective_id = tool_use_id or context.get("tool_use_id", "")
    started_tuids = context.get("_started_tuids") or set()
    # Only warn for sub-agent tuids (those the spawn sites explicitly pre-
    # emitted). The parent scrape tool's own tuid isn't tracked on the
    # context — its card is opened by the parent agent's launcher, not here.
    # Empty `started_tuids` set means no spawn has pre-emitted yet on this
    # context — skip the check.
    if started_tuids and tool_use_id and effective_id not in started_tuids:
        logger.warning(
            "stage_emit_before_agent_started: stage=%s scrape_id=%s tuid=%s "
            "msg=%r · expected pre-emit at spawn site · see "
            "asset-picker-fixes-v6 S2 + v9-live-test-fixes plan",
            stage.value, context.get("scrape_id", ""),
            effective_id, message,
        )

    await emit_progress(context, message, tool_use_id=tool_use_id)
    logger.info(
        "stage=%s scrape_id=%s tuid=%s msg=%r",
        stage.value, context.get("scrape_id", ""),
        effective_id, message,
    )
