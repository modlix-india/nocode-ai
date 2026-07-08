"""Lightweight URL fetch + focused Q&A extraction.

Inspired by Claude Code's ``WebFetch``: the main agent asks a pointed question
about a URL; a cheap subagent (``gpt-4o-mini``) does the actual HTML reading
and returns a concise answer. Keeps the main agent's context lean and lets
one "verification" per candidate competitor be dirt-cheap.

Usage (async):
    answer = await fetch_and_answer(
        url="https://prestigegroup.com/projects/southern-star",
        question="What's the pricing and where is this project located?",
    )
    # -> {"url": ..., "title": ..., "answer": "3-4 BHK apartments, ₹1.2–2.8 Cr, ..."}

Caches ``(url, question)`` pairs in-memory for 15 minutes to match Claude
Code's cache TTL - re-asks against the same page are free.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any

import httpx

from app.agents.adzump.agents.product.adapters.html_parser import parse_html
from app.config import settings

logger = logging.getLogger(__name__)

# ---- Config --------------------------------------------------------------

FETCH_TIMEOUT_S = 10.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MB cap
MAX_MARKDOWN_CHARS = 60_000           # cap markdown we pass to the subagent
CACHE_TTL_S = 15 * 60                 # 15 minutes
# Extractor is swappable via SHORTLIST_CLASSIFIER_PROVIDER env (shared with
# the shortlist classifier). Default ``anthropic`` uses Claude Haiku; set
# to ``openai`` to revert to the legacy gpt-4o-mini path.
EXTRACTOR_OPENAI_MODEL = "gpt-4o-mini"
EXTRACTOR_MAX_TOKENS = 400

USER_AGENT = (
    "Mozilla/5.0 (compatible; AdpilotAnalyst/1.0; +https://modlix.com)"
)

_SUBAGENT_SYSTEM_PROMPT = (
    "You read a webpage and answer a focused question about it. "
    "Rules:\n"
    "- Use ONLY facts present in the page content provided.\n"
    "- Be concise (1-4 sentences or a short bullet list).\n"
    "- Quote specifics (prices, names, numbers) verbatim where possible, "
    "otherwise paraphrase.\n"
    "- If the page doesn't answer the question, say 'not found on this page'.\n"
    "- Never invent URLs, pricing, or features."
)


# ---- In-memory cache -----------------------------------------------------

_CacheKey = str
_cache: dict[_CacheKey, tuple[float, dict[str, Any]]] = {}
_cache_lock = asyncio.Lock()


def _cache_key(url: str, question: str) -> _CacheKey:
    h = hashlib.md5(f"{url}|{question}".encode()).hexdigest()
    return h


async def _cache_get(key: _CacheKey) -> dict[str, Any] | None:
    async with _cache_lock:
        hit = _cache.get(key)
        if not hit:
            return None
        ts, payload = hit
        if time.time() - ts > CACHE_TTL_S:
            _cache.pop(key, None)
            return None
        return payload


async def _cache_put(key: _CacheKey, payload: dict[str, Any]) -> None:
    async with _cache_lock:
        _cache[key] = (time.time(), payload)
        # Very light LRU - cap 256 entries
        if len(_cache) > 256:
            oldest = min(_cache.items(), key=lambda kv: kv[1][0])
            _cache.pop(oldest[0], None)


# ---- Fetch + convert -----------------------------------------------------

def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    return url


async def _fetch_html(url: str) -> tuple[str, str]:
    """GET the URL. Returns (html, final_url). Raises on transport errors."""
    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT_S,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        # Cap body size
        content = resp.content[:MAX_RESPONSE_BYTES]
        encoding = resp.encoding or "utf-8"
        try:
            text = content.decode(encoding, errors="replace")
        except LookupError:
            text = content.decode("utf-8", errors="replace")
        return text, str(resp.url)


def _to_markdown(url: str, html: str) -> tuple[str, str]:
    """Convert HTML to a compact markdown-ish string suitable for an LLM.

    Returns (title, markdown). We reuse the existing parse_html to avoid
    pulling in a second HTML parser dependency.
    """
    page = parse_html(url, html)

    parts: list[str] = []
    if page.title:
        parts.append(f"# {page.title}")
    if page.meta_description:
        parts.append(page.meta_description)

    # Interleave headings + following paragraphs so structure is preserved.
    if page.headings:
        parts.append("\n## Headings")
        for h in page.headings[:40]:
            parts.append(f"- {h}")
    if page.paragraphs:
        parts.append("\n## Content")
        for p in page.paragraphs[:60]:
            snippet = p.strip()
            if snippet:
                parts.append(snippet)

    md = "\n\n".join(parts)
    if len(md) > MAX_MARKDOWN_CHARS:
        md = md[:MAX_MARKDOWN_CHARS] + "\n\n[truncated]"
    return page.title or url, md


# ---- LLM extractor -------------------------------------------------------

async def _extract_via_anthropic(user_prompt: str) -> str:
    """Focused-question extractor using Claude Haiku (default)."""
    import anthropic

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    resp = await asyncio.to_thread(
        client.messages.create,
        model=settings.CLAUDE_HAIKU,
        max_tokens=EXTRACTOR_MAX_TOKENS,
        system=_SUBAGENT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
        temperature=0.2,
    )
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return (getattr(block, "text", "") or "").strip()
    return ""


async def _extract_via_openai(user_prompt: str) -> str:
    """Legacy extractor path - gpt-4o-mini behind the env kill switch."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    resp = await client.chat.completions.create(
        model=EXTRACTOR_OPENAI_MODEL,
        messages=[
            {"role": "system", "content": _SUBAGENT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        max_tokens=EXTRACTOR_MAX_TOKENS,
    )
    if resp.choices and resp.choices[0].message and resp.choices[0].message.content:
        return resp.choices[0].message.content.strip()
    return ""


async def _extract_answer(url: str, title: str, markdown: str, question: str) -> str:
    """Ask a small model the focused question against the page content.

    Provider selection: ``SHORTLIST_CLASSIFIER_PROVIDER`` env - ``anthropic``
    (default) uses Claude Haiku, ``openai`` keeps the legacy gpt-4o-mini path.
    """
    import os

    user_prompt = (
        f"URL: {url}\n"
        f"Title: {title}\n\n"
        f"## Question\n{question}\n\n"
        f"## Page content\n{markdown}"
    )
    provider = (os.getenv("SHORTLIST_CLASSIFIER_PROVIDER") or "anthropic").lower()
    if provider == "openai":
        return await _extract_via_openai(user_prompt)
    return await _extract_via_anthropic(user_prompt)


# ---- Public API ----------------------------------------------------------

async def fetch_and_answer(url: str, question: str) -> dict[str, Any]:
    """Fetch URL and return a focused answer to ``question``.

    Returns dict with:
    - ``url``: resolved final URL
    - ``title``: page title (best effort)
    - ``answer``: the subagent's concise answer (may be empty on failure)
    - ``status``: "ok" | "fetch_failed" | "empty" | "extract_failed"
    - ``error``: optional error message
    """
    url = _normalize_url(url)
    if not url:
        return {"url": "", "title": "", "answer": "", "status": "empty", "error": "empty url"}
    question = (question or "").strip() or "Summarize this page."

    key = _cache_key(url, question)
    cached = await _cache_get(key)
    if cached is not None:
        logger.debug("web_fetch_cache_hit: url=%s", url[:80])
        return cached

    try:
        html, final_url = await _fetch_html(url)
    except Exception as e:
        logger.warning("web_fetch_http_failed: url=%s err=%s", url, str(e)[:200])
        payload = {
            "url": url, "title": "", "answer": "",
            "status": "fetch_failed", "error": f"{type(e).__name__}: {str(e)[:200]}",
        }
        await _cache_put(key, payload)
        return payload

    try:
        title, markdown = _to_markdown(final_url, html)
    except Exception as e:
        logger.warning("web_fetch_parse_failed: url=%s err=%s", url, str(e)[:200])
        payload = {
            "url": final_url, "title": "", "answer": "",
            "status": "fetch_failed", "error": f"parse error: {e}",
        }
        await _cache_put(key, payload)
        return payload

    if not markdown.strip():
        payload = {"url": final_url, "title": title, "answer": "",
                   "status": "empty", "error": "no extractable content"}
        await _cache_put(key, payload)
        return payload

    try:
        answer = await _extract_answer(final_url, title, markdown, question)
    except Exception as e:
        logger.warning("web_fetch_extract_failed: url=%s err=%s", url, str(e)[:200])
        payload = {
            "url": final_url, "title": title, "answer": "",
            "status": "extract_failed", "error": f"{type(e).__name__}: {str(e)[:200]}",
        }
        await _cache_put(key, payload)
        return payload

    payload = {
        "url": final_url,
        "title": title,
        "answer": answer,
        "status": "ok",
    }
    await _cache_put(key, payload)
    return payload
