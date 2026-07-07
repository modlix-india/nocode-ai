"""Shared web-research tools available to ALL Adzump agents.

- ``web_fetch(url, question)``: Claude-Code-style lightweight URL reader.
  GET + HTML→markdown + cheap subagent answers a focused question. Use for
  verifying candidate URLs without the cost of a full Playwright scrape.

The prior Gemini-backed ``web_search`` / ``plan_searches`` tools have been
removed in favour of Anthropic's server-executed ``web_search_20250305``
built-in tool (declared by individual agents via ``builtin_spec``).
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump._shared import emit_progress, short_url

logger = logging.getLogger(__name__)


# ─── UI formatting helpers ─────────────────────────────────────────────────

def _clean_url(url: str) -> str:
    """Strip tracking params (utm_source=*) from citation URLs."""
    if not url:
        return url
    try:
        p = urlparse(url)
        params = {k: v for k, v in parse_qs(p.query).items() if k != "utm_source"}
        return urlunparse(p._replace(query=urlencode(params, doseq=True)))
    except Exception:
        return url


def _short_text(text: str, max_len: int = 60) -> str:
    """Truncate a sentence at word boundary near ``max_len``."""
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= max_len:
        return text
    cut = text[:max_len].rsplit(" ", 1)[0]
    if not cut:
        cut = text[:max_len]
    return cut.rstrip(",.;:") + "…"


# ─── web_fetch ─────────────────────────────────────────────────────────────

async def _web_fetch(params: dict, context: dict) -> ToolResult:
    """Fetch a URL and answer a focused question from its content."""
    from app.agents.adzump.agents.product.adapters.web_fetch_adapter import (
        fetch_and_answer,
    )

    url = (params.get("url") or "").strip()
    question = (params.get("question") or "").strip()
    if not url:
        return ToolResult(success=False, error="url is required.")
    if not question:
        question = "Summarize this page: what is it, who owns it, where, and pricing if any."

    # Always prepend aggregator-detection check so the agent can drop
    # listing/aggregator pages without a hardcoded blocklist.
    augmented_question = (
        "FIRST line of your answer: write either 'TYPE: BRAND' (single project/"
        "brand/company official site) or 'TYPE: AGGREGATOR' (a directory listing "
        "many unrelated brands, comparison portal, or marketplace). Judge from "
        "the page content - many unrelated brand names = aggregator.\n\n"
        f"Then answer this question: {question}"
    )

    await emit_progress(
        context,
        f'web_fetch · {short_url(url)} - {_short_text(question)}',
    )

    result = await fetch_and_answer(url, augmented_question)

    if result.get("status") != "ok":
        return ToolResult(
            success=False,
            error=f"web_fetch failed ({result.get('status')}): {result.get('error', '')}",
            summary=f"Could not read {url}: {result.get('error', 'unknown')}",
        )

    answer = result.get("answer") or ""
    is_aggregator = answer.strip().upper().startswith("TYPE: AGGREGATOR")

    data = {
        "url": result.get("url"),
        "title": result.get("title"),
        "answer": answer,
        "is_aggregator": is_aggregator,
    }
    if is_aggregator:
        summary = (
            f"⚠ AGGREGATOR detected at {result.get('url')} - DO NOT use as a competitor. "
            f"Try another URL from your search results.\n\n{answer[:500]}"
        )
    else:
        summary = (
            f"Read {result.get('url')}\n"
            f"Title: {result.get('title')}\n\n"
            f"Answer to '{question[:120]}':\n{answer}"
        )
    return ToolResult(success=True, data=data, summary=summary)


web_fetch = ToolDefinition(
    name="web_fetch",
    description=(
        "Fetch a URL and get a focused answer from its content (via a cheap "
        "extractor). Use for verifying information about a specific URL: "
        "'What's the pricing?', 'Is this in the right city?', 'What are the "
        "key features?'. Lighter than a full page scrape - no screenshot, "
        "no browser render. Safe to call in parallel for multiple URLs."
    ),
    display_name="Web Fetch",
    parameters=[
        ToolParameter(
            name="url",
            type="string",
            description="Absolute URL to fetch (https://…).",
            required=True,
        ),
        ToolParameter(
            name="question",
            type="string",
            description=(
                "The specific question to answer from this page. Examples: "
                "'What's the pricing and BHK configurations?', "
                "'Is this project in Bangalore?', "
                "'What are their 3 main USPs?'"
            ),
            required=True,
        ),
    ],
    execute=_web_fetch,
)


RESEARCH_TOOLS = [web_fetch]
