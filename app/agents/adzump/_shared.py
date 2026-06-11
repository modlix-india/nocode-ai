"""Shared utilities for adzump tools."""

from __future__ import annotations

import logging
import re as _re
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


def short_url(url: str, max_len: int = 55) -> str:
    """Render a URL compactly for live progress strings shown in the UI.

    - Strips ``www.`` and query/fragment.
    - Keeps host + last 1-2 path segments; elides middle segments as ``/…/``.
    - Hard-caps length, end-truncates with ``…``.

    Display-only — never persist this form. Stored URLs always use the full
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
    Leaves explicit ``http://`` alone — caller decides whether to keep
    or force-upgrade to https (see business_storage._normalize_url for
    the storage-canonicalization concern).
    """
    url = (raw or "").strip()
    if not url:
        return None
    if not url.startswith("http"):
        url = f"https://{url}"
    return url


_IMAGE_KIND_FOLDERS = {
    "screenshot": "screenshots",
    "logo": "logos",
    "creative": "creatives",
    "logo_thumb": "logos",
    "creative_thumb": "creatives",
}

_REHOST_TIMEOUT_S = 5.0
_REHOST_MAX_BYTES = 5 * 1024 * 1024  # 5 MB

# Known image extensions used to recover when a CDN response has no
# content-type header. Browsers fall back to the URL extension; we should too.
_IMAGE_URL_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".avif", ".bmp")


def looks_like_image_response(content_type: str, url: str) -> bool:
    """True if the response is an image. Accepts a proper `image/*` content-
    type OR — when the server didn't set one — a URL ending in a known image
    extension. Some CDNs (cdn.modlix.com is one) serve image bytes with no
    Content-Type header; the browser sniffs them as images, so we should
    too. Anything with a non-image content-type is always rejected."""
    ct = (content_type or "").lower().split(";", 1)[0].strip()
    if ct.startswith("image/"):
        return True
    if ct:  # has a content-type but it's not image/* — definitely not an image
        return False
    path = url.lower().split("?", 1)[0]
    return path.endswith(_IMAGE_URL_EXTS)


def _guess_ctype_from_url(url: str) -> str:
    """Synthesize an `image/<ext>` content-type from a URL when the response
    didn't include one. Used by the upload helper to set a reasonable
    filename suffix downstream."""
    path = url.lower().split("?", 1)[0]
    for ext in _IMAGE_URL_EXTS:
        if path.endswith(ext):
            suffix = ext.lstrip(".")
            # Normalize a couple of cases that differ between ext and MIME.
            if suffix == "jpg":
                suffix = "jpeg"
            return f"image/{suffix}"
    return "image/jpeg"


async def upload_image(
    image_bytes: bytes,
    filename: str,
    kind: str,
    context: dict,
    content_type: str = "application/octet-stream",
) -> str | None:
    """Upload an image to the gateway files API under the folder for `kind`.

    `kind` ∈ {"screenshot", "logo", "creative"}.
    `content_type` is what we declare in the multipart form so the gateway
    stores it correctly — without this the form was hardcoded to image/jpeg
    and SVG / WebP uploads were getting mis-labeled.
    """
    folder = _IMAGE_KIND_FOLDERS.get(kind, "screenshots")
    ct = (content_type or "application/octet-stream").split(";", 1)[0].strip()
    try:
        import httpx

        from app.config import settings
        headers = build_ds_headers(context)
        headers["accept"] = "application/json"
        client_code = context.get("client_code", "")
        base = settings.GATEWAY_URL

        file_headers = {
            "Authorization": headers.get("Authorization", ""),
            "ClientCode": client_code,
            "AppCode": headers.get("appCode", "appbuilder"),
            "X-Forwarded-Host": headers.get("X-Forwarded-Host", "localhost"),
            "X-Forwarded-Port": headers.get("X-Forwarded-Port", "80"),
            "accept": "application/json",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(
                f"{base}/api/files/static/directory/{folder}",
                headers=file_headers,
            )
            response = await client.post(
                f"{base}/api/files/static/{folder}?clientCode={client_code}",
                headers=file_headers,
                files={"file": (filename, image_bytes, ct)},
            )
            if response.status_code == 200:
                data = response.json()
                upload_url = data.get("url", "")
                if upload_url:
                    # File service returns a path relative to its own API root
                    # (e.g. "api/files/static/file/X/creatives/foo.webp"). The
                    # browser resolves an <img src="api/..."> against whatever
                    # path the host page is on — `/marketingai/SYSTEM/page/X/`
                    # — which 404s. Emit the absolute URL so consumers don't
                    # have to know the gateway host.
                    if not upload_url.startswith(("http://", "https://")):
                        upload_url = f"{base.rstrip('/')}/{upload_url.lstrip('/')}"
                    logger.info("image_uploaded: kind=%s url=%s", kind, upload_url)
                    return upload_url
            logger.warning(
                "image_upload_failed: kind=%s status=%d body=%s",
                kind, response.status_code, response.text[:200],
            )
    except Exception as e:
        logger.warning("image_upload_error: kind=%s err=%s", kind, str(e)[:200])
    return None


async def upload_screenshot(screenshot_bytes: bytes, filename: str, context: dict) -> str | None:
    """Backward-compat wrapper around upload_image(kind='screenshot')."""
    return await upload_image(screenshot_bytes, filename, "screenshot", context)


# content-type → file extension. Generic "image/svg+xml" naturally becomes
# "svg+xml" via str.split("/")[1] which breaks the filename — map explicitly.
_CTYPE_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/svg+xml": "svg",
    "image/x-icon": "ico",
    "image/vnd.microsoft.icon": "ico",
    "image/avif": "avif",
}


def _ext_for_content_type(content_type: str) -> str:
    """Stable file extension for a content-type. Falls back to "bin" so we
    never emit a filename with an unsafe character (`+`, `;`) in the suffix."""
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    return _CTYPE_EXT.get(ct, "bin")


async def upload_and_analyze(
    image_bytes: bytes,
    content_type: str,
    source_url: str,
    kind: str,
    context: dict,
    hints: dict | None = None,
) -> dict | None:
    """Upload bytes + attach render hints. Returns {url, format, **hints} or
    None on upload failure. Hints (`background`, `fit`) are passed in by the
    caller — typically derived from the vision LLM that already inspected the
    thumbnail to pick the asset. Empty/None hints just produce a {url, format}
    block; the UI renders that on its neutral default tile."""
    from hashlib import md5

    ext = _ext_for_content_type(content_type)
    filename = f"{kind}_{md5(source_url.encode()).hexdigest()[:12]}.{ext}"
    url = await upload_image(image_bytes, filename, kind, context, content_type)
    if not url:
        return None
    clean_hints = {k: v for k, v in (hints or {}).items() if v}
    logger.info(
        "upload_and_analyze: kind=%s url=%s format=%s hints=%s bytes=%d",
        kind, url, ext, clean_hints, len(image_bytes),
    )
    return {"url": url, "format": ext, **clean_hints}


async def rehost_image(
    source_url: str, kind: str, context: dict, hints: dict | None = None,
) -> dict | None:
    """Download an image and re-host on our service, attaching render hints.

    Third-party CDN URLs rot — re-hosting gives creative-gen a stable URL.
    `hints` (`background`, `fit`) are passed through to the upload record
    so the UI can render with the right tile contrast; the LLM that picked
    the asset is the source of truth for those, not pixel sampling here.

    Returns {url, format, **hints} on success. None on any failure
    (timeout, non-image, oversize, upload failure)."""
    if not source_url:
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=_REHOST_TIMEOUT_S, follow_redirects=True) as client:
            resp = await client.get(source_url)
            if resp.status_code != 200:
                logger.info("rehost_skip: status=%d url=%s", resp.status_code, source_url[:200])
                return None
            raw_ctype = resp.headers.get("content-type") or ""
            if not looks_like_image_response(raw_ctype, source_url):
                logger.info("rehost_skip: ctype=%s url=%s", raw_ctype, source_url[:200])
                return None
            ctype = (raw_ctype or _guess_ctype_from_url(source_url)).lower().split(";", 1)[0].strip()
            data = resp.content
            if not data or len(data) > _REHOST_MAX_BYTES:
                logger.info("rehost_skip: size=%d url=%s", len(data or b""), source_url[:200])
                return None
    except Exception as e:
        logger.info("rehost_fetch_failed: url=%s err=%s", source_url[:200], str(e)[:200])
        return None

    logger.info(
        "rehost_fetched: kind=%s bytes=%d ctype=%s src=%s",
        kind, len(data), ctype, source_url[:200],
    )
    return await upload_and_analyze(data, ctype, source_url, kind, context, hints)


import json as _json

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


# ─── Shared progress emission ────────────────────────────────────────────

async def emit_progress(
    context: dict[str, Any], message: str, tool_use_id: str | None = None,
) -> None:
    """Fire-and-forget progress update for a tool row.

    Reads ``event_stream`` from the context. Tool-use-id resolution:
      · If ``tool_use_id`` kwarg is provided (non-None, non-empty), use it.
        Callers pass this to override the default (e.g., a sub-agent
        attributing its own stage emits to its own row instead of the
        parent tool's row — see asset-picker-fixes-v4 I-1).
      · Else fall back to ``context["tool_use_id"]`` (the BaseAgent loop
        injects this at tool-call time).
    Safe to call even when either stream or id is missing — swallows all
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
