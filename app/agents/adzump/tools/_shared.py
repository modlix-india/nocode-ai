"""Shared utilities for adzump tools."""

from __future__ import annotations

import logging
from typing import Any

from app.core.tools.base import ToolResult

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


async def upload_screenshot(
    screenshot_bytes: bytes, filename: str, context: dict
) -> str | None:
    """Upload screenshot to file service, return URL.

    Uses the gateway files API. Creates screenshots folder if needed.
    """
    try:
        import httpx

        from app.config import settings

        headers = build_ds_headers(context)
        headers["accept"] = "application/json"
        client_code = context.get("client_code", "")
        base = settings.GATEWAY_URL

        # Use auth headers from context — same appCode the token was issued for
        file_headers = {
            "Authorization": headers.get("Authorization", ""),
            "ClientCode": client_code,
            "AppCode": headers.get("appCode", "appbuilder"),
            "X-Forwarded-Host": headers.get("X-Forwarded-Host", "localhost"),
            "X-Forwarded-Port": headers.get("X-Forwarded-Port", "80"),
            "accept": "application/json",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Ensure screenshots folder exists
            await client.post(
                f"{base}/api/files/static/directory/screenshots",
                headers=file_headers,
            )

            # Upload file
            response = await client.post(
                f"{base}/api/files/static/screenshots?clientCode={client_code}",
                headers=file_headers,
                files={"file": (filename, screenshot_bytes, "image/jpeg")},
            )
            if response.status_code == 200:
                data = response.json()
                upload_url = data.get("url", "")
                if upload_url:
                    logger.info("screenshot_uploaded: url=%s", upload_url)
                    return upload_url
            logger.warning(
                "screenshot_upload_failed: status=%d body=%s",
                response.status_code,
                response.text[:200],
            )
    except Exception as e:
        logger.warning("screenshot_upload_error: %s", str(e)[:200])
    return None


import json as _json
import re as _re

_JSON_BLOCK_RE = _re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", _re.DOTALL)


def extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of an LLM response (```json ...``` or raw)."""
    if not text:
        return None
    match = _JSON_BLOCK_RE.search(text)
    candidate = match.group(1) if match else None
    if candidate is None:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            candidate = text[start : end + 1]
    if not candidate:
        return None
    try:
        return _json.loads(candidate)
    except _json.JSONDecodeError:
        return None


def require_campaign_spec(context: dict, *fields: str) -> ToolResult | None:
    """Validate that required campaign-spec fields exist in session context.

    Returns a ToolResult error if any field is missing, or None if all present.
    """
    spec = context.get("session_context", {}).get("campaign_spec", {})
    missing = [f for f in fields if not spec.get(f)]
    if missing:
        return ToolResult(
            success=False,
            error=f"Missing required campaign-spec fields: {', '.join(missing)}. "
            "Collect this information from the user first.",
        )
    return None


# ─── Shared URL / host helpers ────────────────────────────────────────────
# Single source of truth for aggregator/portal/social domains. Tools that
# need a narrower or broader set can extend it (e.g. comp_discovery adds
# google.com for Maps-citation URLs; competitor.py adds content platforms).

AGGREGATOR_HOSTS: frozenset[str] = frozenset(
    {
        # Real-estate / property portals
        "99acres.com",
        "magicbricks.com",
        "housing.com",
        "squareyards.com",
        "commonfloor.com",
        "nobroker.in",
        "makaan.com",
        "proptiger.com",
        "nestaway.com",
        # Local-services / reviews / listings
        "yelp.com",
        "tripadvisor.com",
        "zomato.com",
        "swiggy.com",
        "justdial.com",
        "sulekha.com",
        "glassdoor.com",
        "indeed.com",
        # Marketplaces
        "amazon.com",
        "amazon.in",
        "flipkart.com",
        "indiamart.com",
        # SaaS/product aggregators
        "g2.com",
        "capterra.com",
        "producthunt.com",
        "trustpilot.com",
        # Social / forums / wiki
        "reddit.com",
        "quora.com",
        "youtube.com",
        "facebook.com",
        "instagram.com",
        "twitter.com",
        "x.com",
        "wikipedia.org",
        "linkedin.com",
        "pinterest.com",
    }
)


def host_of(url: str | None) -> str:
    """Normalized host (lowercased, www. stripped) for a URL. Empty on failure."""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse

        return (urlparse(url).netloc or "").lower().removeprefix("www.")
    except Exception:
        return ""


def is_aggregator_host(
    host: str, extra_hosts: frozenset[str] | set[str] = frozenset()
) -> bool:
    """True if host matches AGGREGATOR_HOSTS or its extensions (``a.example.com``
    matches ``example.com``). Callers may pass ``extra_hosts`` for domain-specific
    additions (e.g. ``{"google.com"}`` for Maps-citation URLs)."""
    if not host:
        return False
    all_hosts = AGGREGATOR_HOSTS | frozenset(extra_hosts)
    return any(host == h or host.endswith("." + h) for h in all_hosts)


# ─── Shared progress emission ────────────────────────────────────────────


async def emit_progress(
    context: dict[str, Any],
    message: str,
) -> None:
    """Fire-and-forget progress update for the current tool row.

    Reads ``event_stream`` and ``tool_use_id`` from the tool context (the
    BaseAgent loop injects these at call time). Safe to call even when
    either is missing — swallows all exceptions so tools can use it
    unconditionally.
    """
    stream = context.get("event_stream")
    tool_use_id = context.get("tool_use_id", "")
    if not stream or not tool_use_id:
        return
    try:
        await stream.emit_tool_update(tool_use_id, message)
    except Exception:
        pass
