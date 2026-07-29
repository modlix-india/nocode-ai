"""Shared utilities for adzump tools."""

from __future__ import annotations

import logging
import re as _re
from typing import Any

logger = logging.getLogger(__name__)


def build_ds_headers(context: dict) -> dict[str, str]:
    """Build HTTP headers for ds service calls from tool context.

    Forwards auth token and client code so ds can authenticate
    with Google Ads / Meta APIs on behalf of the user.
    """
    headers = {}
    if "headers" in context:
        # Forward all auth headers from the session
        headers.update(context["headers"])
    if "client_code" in context:
        headers["clientCode"] = context["client_code"]
    return headers


# CoreServices.Storage endpoints - one home for the gateway contract, shared by
# every storage-backed module (business_storage, creative_intelligence.store).
STORAGE_READ_PAGE = "/api/core/function/execute/CoreServices.Storage/ReadPage"
STORAGE_CREATE = "/api/core/function/execute/CoreServices.Storage/Create"
STORAGE_UPDATE = "/api/core/function/execute/CoreServices.Storage/Update"


def storage_headers(ctx: dict, app_code: str, client_code: str | None = None) -> dict[str, str]:
    """Headers for CoreServices.Storage calls. ``app_code`` pins the collection's
    appCode scope. ``client_code`` overrides the session's clientCode (e.g. the
    SYSTEM-shared creative library); omitted, the user's own scope applies."""
    h = build_ds_headers(ctx)
    if client_code:
        h["clientCode"] = client_code
    h["AppCode"] = app_code
    h["Content-Type"] = "application/json"
    return h


def extract_storage_records(raw: Any) -> list[dict]:
    """Unwrap CoreServices.Storage's response envelope into a flat record list.
    The gateway wraps the storage result in two ``result`` levels, then either
    has ``content`` (paged) or returns records directly. Tolerates both. Shared
    by every storage-backed module (business_storage, creative_intelligence)."""
    if raw is None:
        return []
    data = raw
    if isinstance(data, list) and data:
        data = data[0]
    for _ in range(2):  # 2-level unwrap of the known result.result envelope
        if isinstance(data, dict) and "result" in data:
            data = data["result"]
        else:
            break
    if data is None:
        return []
    if isinstance(data, dict) and "content" in data:
        content = data["content"]
        return content if isinstance(content, list) else [content]
    return data if isinstance(data, list) else [data]


def short_url(url: str, max_len: int = 55) -> str:
    """Render a URL compactly for live progress strings shown in the UI.

    - Strips ``www.`` and query/fragment.
    - Keeps host + last 1-2 path segments; elides middle segments as ``/…/``.
    - Hard-caps length, end-truncates with ``…``.

    Display-only - never persist this form. Stored URLs always use the full
    URL (see business_storage._normalize_url for the storage-canonical form).
    """
    from urllib.parse import urlparse
    if not url:
        return ""
    try:
        p = urlparse(url)
        host = (p.netloc or "").removeprefix("www.")
        if not host:
            return url[:max_len]
        path = (p.path or "").rstrip("/")
        parts = [seg for seg in path.split("/") if seg]
        if not parts:
            display = host
        elif len(parts) <= 2:
            display = f"{host}/{'/'.join(parts)}"
        else:
            display = f"{host}/…/{parts[-1]}"
        if len(display) > max_len:
            display = display[: max_len - 1] + "…"
        return display
    except Exception:
        return url[:max_len]


def clean_input_url(raw) -> str | None:
    """Tool-input boundary cleaner for URLs the LLM passes in.

    Trims whitespace, defaults the scheme to ``https://`` if missing,
    and returns ``None`` when the input is empty or whitespace-only.
    Leaves explicit ``http://`` alone - caller decides whether to keep
    or force-upgrade to https (see business_storage._normalize_url for
    the storage-canonicalization concern).
    """
    url = (raw or "").strip()
    if not url:
        return None
    if not url.startswith("http"):
        url = f"https://{url}"
    return url


import json as _json

_JSON_FENCE_RE = _re.compile(r"```json\s*\n(.*?)\n```", _re.DOTALL)


def extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of an LLM response. The one parser for
    every final-JSON sub-agent (vision, creative_essence, product) - handles a
    ```json fence, a fence without a language tag, a bare object, and falls back
    to the first {...} span. None when nothing parses."""
    if not text:
        return None
    m = _JSON_FENCE_RE.search(text)
    raw = m.group(1) if m else text.strip()
    # Strip stray code fences if the model emitted ``` without a language tag.
    raw = _re.sub(r"^```[a-z]*\s*", "", raw)
    raw = _re.sub(r"\s*```\s*$", "", raw)
    try:
        payload = _json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except _json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = _json.loads(raw[start : end + 1])
    except _json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


# ─── Shared URL / host helpers ────────────────────────────────────────────
# Single source of truth for aggregator/portal/social domains. Tools that
# need a narrower or broader set can extend it (e.g. comp_discovery adds
# google.com for Maps-citation URLs; competitor.py adds content platforms).

AGGREGATOR_HOSTS: frozenset[str] = frozenset({
    # Real-estate / property portals
    "99acres.com", "magicbricks.com", "housing.com", "squareyards.com",
    "commonfloor.com", "nobroker.in", "makaan.com", "proptiger.com",
    "nestaway.com",
    # Local-services / reviews / listings
    "yelp.com", "tripadvisor.com", "zomato.com", "swiggy.com",
    "justdial.com", "sulekha.com", "glassdoor.com", "indeed.com",
    # Marketplaces
    "amazon.com", "amazon.in", "flipkart.com", "indiamart.com",
    # SaaS/product aggregators
    "g2.com", "capterra.com", "producthunt.com", "trustpilot.com",
    # Social / forums / wiki
    "reddit.com", "quora.com", "youtube.com", "facebook.com",
    "instagram.com", "twitter.com", "x.com", "wikipedia.org",
    "linkedin.com", "pinterest.com",
})


def host_of(url: str | None) -> str:
    """Normalized host (lowercased, www. stripped) for a URL. Empty on failure."""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        return (urlparse(url).netloc or "").lower().removeprefix("www.")
    except Exception:
        return ""


def is_aggregator_host(host: str, extra_hosts: frozenset[str] | set[str] = frozenset()) -> bool:
    """True if host matches AGGREGATOR_HOSTS or its extensions (``a.example.com``
    matches ``example.com``). Callers may pass ``extra_hosts`` for domain-specific
    additions (e.g. ``{"google.com"}`` for Maps-citation URLs)."""
    if not host:
        return False
    all_hosts = AGGREGATOR_HOSTS | frozenset(extra_hosts)
    return any(host == h or host.endswith("." + h) for h in all_hosts)


# ─── Shared product-data helpers ─────────────────────────────────────────

def product_location_str(product_data: dict) -> str:
    """The business address from product_data.place (the LLM's wire shapes are
    normalized into place at the merge boundary - tools/product.py)."""
    return ((product_data.get("place") or {}).get("address") or "").strip()


def primary_screenshot_url(product_data: dict) -> str:
    """The primary page's full-page screenshot upload; "" until scraped.
    Derived from pages[primary_url] - the single home of per-page state."""
    pages = product_data.get("pages") or {}
    page = pages.get(product_data.get("primary_url") or "") or {}
    return page.get("screenshot_url") or ""


# ─── Shared progress emission ────────────────────────────────────────────

async def emit_progress(
    context: dict[str, Any], message: str, tool_use_id: str | None = None,
) -> None:
    """Fire-and-forget progress update for a tool row.

    Reads ``event_stream`` from the context. Tool-use-id resolution:
      · If ``tool_use_id`` kwarg is provided (non-None, non-empty), use it.
        Callers pass this to override the default (e.g., a sub-agent
        attributing its own stage emits to its own row instead of the
        parent tool's row - see asset-picker-fixes-v4 I-1).
      · Else fall back to ``context["tool_use_id"]`` (the BaseAgent loop
        injects this at tool-call time).
    Safe to call even when either stream or id is missing - swallows all
    exceptions so tools can use it unconditionally.
    """
    stream = context.get("event_stream")
    effective_id = tool_use_id or context.get("tool_use_id", "")
    if not stream or not effective_id:
        return
    try:
        await stream.emit_tool_update(effective_id, message)
    except Exception:
        pass
