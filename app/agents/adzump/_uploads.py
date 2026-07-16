"""Image upload + rehost pipeline for adzump.

Moved out of `_shared.py` (which became a 6-concern junk drawer). This is the
transport layer: push image bytes to the gateway files API, guess content
types, rehost remote URLs. Knows nothing about the asset domain (roles, the
product_data lists) - that's the picker's / T-014's concern.
"""

from __future__ import annotations

import logging
import re
from hashlib import md5

from app.agents.adzump._shared import build_ds_headers, host_of

logger = logging.getLogger(__name__)


_IMAGE_KIND_FOLDERS = {
    "screenshot": "screenshots",
    "logo": "logos",
    "creative": "creatives",
    "logo_thumb": "logos",
    "creative_thumb": "creatives",
    # Competitor ad creatives rehosted from adlibrary.com into our file store
    # so the shared library doesn't depend on adlibrary's (undocumented-TTL) URLs.
    "competitor_creative": "competitor-creatives",
}

_REHOST_TIMEOUT_S = 5.0
_REHOST_MAX_BYTES = 5 * 1024 * 1024  # 5 MB

# Known image extensions used to recover when a CDN response has no
# content-type header. Browsers fall back to the URL extension; we should too.
_IMAGE_URL_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".avif", ".bmp")


def looks_like_image_response(content_type: str, url: str) -> bool:
    """True if the response is an image. Accepts a proper `image/*` content-
    type OR - when the server didn't set one - a URL ending in a known image
    extension. Some CDNs (cdn.modlix.com is one) serve image bytes with no
    Content-Type header; the browser sniffs them as images, so we should
    too. Anything with a non-image content-type is always rejected."""
    ct = (content_type or "").lower().split(";", 1)[0].strip()
    if ct.startswith("image/"):
        return True
    if ct:  # has a content-type but it's not image/* - definitely not an image
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
    stores it correctly - without this the form was hardcoded to image/jpeg
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
                    # (e.g. "api/files/static/file/X/creatives/foo.webp"). A
                    # bare relative path 404s on deep page paths
                    # (`/marketingai/SYSTEM/page/X/`), and prefixing
                    # GATEWAY_URL bakes in the cluster-internal hostname on
                    # dev (gateway-server:8080) which the browser can't reach.
                    # Root-relative resolves against the page origin
                    # everywhere (dev ingress + local /api proxy).
                    if not upload_url.startswith(("http://", "https://", "/")):
                        upload_url = f"/{upload_url}"
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
# "svg+xml" via str.split("/")[1] which breaks the filename - map explicitly.
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


def _asset_filename(
    context: dict, name: str, kind: str, image_bytes: bytes, content_type: str,
) -> str:
    """The one place an asset filename is built: <product>_<name|kind>_<hash6>.<ext>.

    `name` comes from the LLM that saw the image - slugified here, never
    trusted raw. Product prefix = product_name (host fallback, since the
    profile writer hasn't merged during a scrape). Hash is of the BYTES, not
    the url - every pasted upload is "image.png", so a url-hash collided and
    the file store served stale images.
    """
    def slug(s: str, n: int) -> str:  # trust boundary - LLM strings never hit the fs raw
        return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:n].rstrip("-")

    pd = (context.get("session_context") or {}).get("product_data") or {}
    product = pd.get("product_name") or ""
    if not product and pd.get("primary_url"):
        product = host_of(pd["primary_url"]).split(".")[0]
    parts = [p for p in (slug(product, 30), slug(name, 40) or kind) if p]
    ext = _ext_for_content_type(content_type)
    return f"{'_'.join(parts)}_{md5(image_bytes).hexdigest()[:6]}.{ext}"


async def upload_and_analyze(
    image_bytes: bytes,
    content_type: str,
    source_url: str,
    kind: str,
    context: dict,
    hints: dict | None = None,
    name: str = "",
    perceptual: bool = False,
) -> dict | None:
    """Upload bytes + attach render hints. Returns {url, format, contentHash,
    **hints} or None on upload failure. Hints (`background`, `fit`) are passed in
    by the caller - typically derived from the vision LLM that already inspected
    the thumbnail to pick the asset. Empty/None hints just produce a {url, format}
    block; the UI renders that on its neutral default tile.

    `name` = semantic name from the LLM that saw the image ('project-logo',
    'floor-plan-3bhk'); see `_asset_filename` for the scheme.

    `perceptual` adds a `perceptualHash` (DCT hash of the decoded image) to the
    result - only the competitor-creative dedup path needs it, so it's off by
    default (product/logo uploads skip the decode). md5 stays on the raw bytes;
    the perceptual hash decodes the image once."""
    ext = _ext_for_content_type(content_type)
    filename = _asset_filename(context, name, kind, image_bytes, content_type)
    url = await upload_image(image_bytes, filename, kind, context, content_type)
    if not url:
        return None
    clean_hints = {k: v for k, v in (hints or {}).items() if v}
    # Full content hash of the bytes - the dedup + essence-cache key for the
    # creative library (the filename uses the first 6 chars of the same md5).
    content_hash = md5(image_bytes).hexdigest()
    result = {"url": url, "format": ext, "contentHash": content_hash, **clean_hints}
    if perceptual:
        # Lazy import: keeps this generic util off the creative_intelligence
        # package init (no import cycle), and a missing dep just yields "".
        from app.agents.adzump.creative_intelligence.phash import compute_phash
        result["perceptualHash"] = compute_phash(image_bytes)
    logger.info(
        "upload_and_analyze: kind=%s url=%s format=%s hints=%s bytes=%d perceptual=%s",
        kind, url, ext, clean_hints, len(image_bytes), perceptual,
    )
    return result


async def rehost_image(
    source_url: str, kind: str, context: dict, hints: dict | None = None,
    name: str = "", perceptual: bool = False,
) -> dict | None:
    """Download an image and re-host on our service, attaching render hints.

    Third-party CDN URLs rot - re-hosting gives creative-gen a stable URL.
    `hints` (`background`, `fit`) are passed through to the upload record
    so the UI can render with the right tile contrast; the LLM that picked
    the asset is the source of truth for those, not pixel sampling here.

    `perceptual=True` also returns a `perceptualHash` (the competitor-creative
    dedup path sets this; other callers skip the decode).

    Returns {url, format, contentHash, **hints} on success. None on any failure
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
    return await upload_and_analyze(
        data, ctype, source_url, kind, context, hints, name=name, perceptual=perceptual,
    )
