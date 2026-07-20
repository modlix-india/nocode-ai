"""Deterministic web-page analyzer (Playwright + CDP, no LLM in analysis).

Walks a live page's DOM, reads the AUTHORED CSS that applies to each element
across desktop/tablet/mobile, segments it into sections, and emits a
build-ready, Modlix-shaped component plan (`analysis.json`) plus per-section
screenshots. The plan is the input to a later mapping/build step.

Public entry point: `analyze_page(url, ...)`.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Dict, List, Optional

from app.services.page_analyzer.browser import (
    DEFAULT_BREAKPOINTS,
    handle_slug,
    observe_breakpoints,
)
from app.services.page_analyzer.models import (
    BreakpointInfo,
    NodeBreakpoint,
    NodeObservation,
    PageAnalysis,
    Rect,
)

logger = logging.getLogger(__name__)

__all__ = [
    "analyze_page",
    "PageAnalysis",
    "NodeObservation",
    "NodeBreakpoint",
    "BreakpointInfo",
    "Rect",
    "DEFAULT_BREAKPOINTS",
]

_DEFAULT_RUNS_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "runs",
    "page_analyzer",
)


def _resolve_run_dir(url: str, out_dir: Optional[str]) -> str:
    if out_dir:
        run_dir = out_dir
    else:
        ts = time.strftime("%Y%m%d-%H%M%S")
        run_dir = os.path.join(_DEFAULT_RUNS_ROOT, f"{handle_slug(url)}_{ts}")
    os.makedirs(run_dir, exist_ok=True)
    return run_dir


async def analyze_page(
    url: str,
    *,
    out_dir: Optional[str] = None,
    stage: str = "m1",
    breakpoints: Optional[List[Dict[str, object]]] = None,
    headless: bool = True,
    wait_ms: int = 2500,
    use_llm: bool = False,
    in_path: Optional[str] = None,
    write: bool = True,
) -> PageAnalysis:
    """Analyze a page and (optionally) write `analysis.json` to the run dir.

    `stage` controls how far the pipeline runs:
      - "m1": stamp + observe across breakpoints (visibility/box/active-media).
      - later: "m2".."m5" / "all" as milestones land.
    """
    # `render` re-renders an existing analysis.json with no crawl.
    if stage == "render":
        from app.services.page_analyzer.render import render_preview_html

        src = in_path or os.path.join(out_dir or "", "analysis.json")
        with open(src, encoding="utf-8") as fh:
            analysis = PageAnalysis(**json.load(fh))
        analysis.run_dir = analysis.run_dir or os.path.dirname(src)
        preview_path = os.path.join(analysis.run_dir, "preview.html")
        with open(preview_path, "w", encoding="utf-8") as fh:
            fh.write(render_preview_html(analysis))
        logger.info("wrote %s", preview_path)
        return analysis

    run_dir = _resolve_run_dir(url, out_dir)

    if stage == "m1":
        analysis = await observe_breakpoints(
            url, breakpoints=breakpoints, headless=headless, wait_ms=wait_ms
        )
    elif stage == "m2":
        from app.services.page_analyzer.browser import extract_authored_sample

        sample = await extract_authored_sample(url, headless=headless, wait_ms=wait_ms)
        analysis = PageAnalysis(
            url=url,
            analyzed_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            stage="m2",
            extra={"authored_sample": sample},
        )
    elif stage == "m3":
        from app.services.page_analyzer.browser import analyze_structure

        analysis = await analyze_structure(url, headless=headless, wait_ms=wait_ms)
    elif stage == "m4":
        from app.services.page_analyzer.browser import run_pipeline

        analysis = await run_pipeline(
            url, headless=headless, wait_ms=wait_ms, use_llm=use_llm,
            with_shots=True, shots_dir=os.path.join(run_dir, "shots"), stage="m4",
        )
    elif stage in ("m5", "all"):
        from app.services.page_analyzer.browser import run_pipeline

        analysis = await run_pipeline(
            url, headless=headless, wait_ms=wait_ms, use_llm=use_llm,
            with_styles=True, with_visibility=True, with_shots=True,
            breakpoints=breakpoints, shots_dir=os.path.join(run_dir, "shots"),
            stage="m5",
        )
    elif stage == "full":
        from app.services.page_analyzer.browser import run_full_dom

        analysis = await run_full_dom(
            url, headless=headless, wait_ms=wait_ms, use_llm=use_llm,
            breakpoints=breakpoints,
        )
    else:
        raise ValueError(f"unknown/not-yet-implemented stage: {stage}")

    analysis.run_dir = run_dir
    analysis.stage = stage

    if write:
        out_path = os.path.join(run_dir, "analysis.json")
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(analysis.model_dump_json(indent=2))
        logger.info("wrote %s", out_path)
        if analysis.sections or analysis.full_tree is not None:
            from app.services.page_analyzer.render import render_preview_html

            preview_path = os.path.join(run_dir, "preview.html")
            with open(preview_path, "w", encoding="utf-8") as fh:
                fh.write(render_preview_html(analysis))
            logger.info("wrote %s", preview_path)

    return analysis
