"""Asset resolver — turns screenshot-derived asset placeholders into real URLs.

Three lanes:
  1. Icons → mapped to FontAwesome/Material Icons class strings (no upload)
  2. Logos → SimpleIcons / Clearbit lookup, fallback to screenshot crop
  3. Images → cropped from screenshot, uploaded to Modlix files API

All binary assets are uploaded to /api/files/static/file/{client}/{app}/.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import uuid
from typing import Any

import httpx
from PIL import Image

logger = logging.getLogger(__name__)

# FontAwesome class prefix detection
_FA_PREFIXES = ("fa ", "fa-", "fas ", "far ", "fab ", "fal ", "fad ")
_MI_PREFIXES = ("mi ", "material-icons")
_MS_PREFIXES = ("ms ", "material-symbols")

# Map icon pack class prefixes to Modlix pack names
ICON_PACK_MAP = {
    "fa": "FREE_FONT_AWESOME_ALL",
    "fas": "FREE_FONT_AWESOME_ALL",
    "far": "FREE_FONT_AWESOME_ALL",
    "fab": "FREE_FONT_AWESOME_ALL",
    "mi": "MATERIAL_ICONS_FILLED",
    "material-icons": "MATERIAL_ICONS_FILLED",
    "ms": "MATERIAL_SYMBOLS_OUTLINED",
    "material-symbols-outlined": "MATERIAL_SYMBOLS_OUTLINED",
    "material-symbols-rounded": "MATERIAL_SYMBOLS_ROUNDED",
    "material-symbols-sharp": "MATERIAL_SYMBOLS_SHARP",
}


def detect_icon_packs(comp_def: dict[str, Any]) -> set[str]:
    """Scan all Icon components and return the set of required iconpack names."""
    packs: set[str] = set()
    for comp in comp_def.values():
        if comp.get("type") != "Icon":
            continue
        icon_val = comp.get("properties", {}).get("icon", {}).get("value", "")
        if not icon_val:
            continue
        first_token = icon_val.split()[0] if icon_val else ""
        if first_token in ICON_PACK_MAP:
            packs.add(ICON_PACK_MAP[first_token])
        elif any(icon_val.startswith(p) for p in _FA_PREFIXES):
            packs.add("FREE_FONT_AWESOME_ALL")
        elif any(icon_val.startswith(p) for p in _MI_PREFIXES):
            packs.add("MATERIAL_ICONS_FILLED")
        elif any(icon_val.startswith(p) for p in _MS_PREFIXES):
            packs.add("MATERIAL_SYMBOLS_OUTLINED")
    return packs


async def resolve_assets(
    assets: list[dict[str, Any]],
    full_screenshot: Image.Image,
    slices_y_offsets: dict[int, int],   # slice_index → y_start
    api_client: Any,
    headers: dict[str, str],
    app_code: str,
    client_code: str,
) -> dict[str, str]:
    """Resolve asset placeholders to real URLs.

    Args:
        assets: List of {placeholder, kind, bbox, label, dominant_color, brand_hint}
        full_screenshot: PIL Image of the full original screenshot
        slices_y_offsets: Map from slice index to y_start for bbox translation
        api_client: SaasClient instance
        headers: Auth headers
        app_code: Application code
        client_code: Client code

    Returns:
        Dict of placeholder → resolved URL.
    """
    resolved: dict[str, str] = {}
    cache: dict[str, str] = {}  # SHA256 → URL (dedup uploads)
    stats = {"simpleicons": 0, "clearbit": 0, "crop": 0, "failed": 0}

    for asset in assets:
        placeholder = asset.get("placeholder", "")
        kind = asset.get("kind", "image")
        label = asset.get("label", "")
        brand_hint = asset.get("brand_hint", "")
        bbox = asset.get("bbox")
        slice_index = asset.get("slice_index", 0)

        url = None

        # Lane 1: Logo lookup
        if kind == "logo" and (brand_hint or label):
            url = await _try_logo_lookup(brand_hint or label)
            if url:
                # Download and upload to files API
                logo_bytes = await _download_url(url)
                if logo_bytes:
                    mime = "image/svg+xml" if url.endswith(".svg") or "simpleicons" in url else "image/png"
                    uploaded = await _upload_to_files(
                        logo_bytes, mime, app_code, client_code,
                        api_client, headers, cache,
                    )
                    if uploaded:
                        resolved[placeholder] = uploaded
                        stats["simpleicons" if "simpleicons" in (url or "") else "clearbit"] += 1
                        continue

        # Lane 2: Crop from screenshot (always-works fallback)
        if bbox and len(bbox) == 4:
            y_offset = slices_y_offsets.get(slice_index, 0)
            crop_bytes = _crop_from_screenshot(
                full_screenshot, bbox, y_offset,
            )
            if crop_bytes:
                uploaded = await _upload_to_files(
                    crop_bytes, "image/png", app_code, client_code,
                    api_client, headers, cache,
                )
                if uploaded:
                    resolved[placeholder] = uploaded
                    stats["crop"] += 1
                    continue

        # If all else fails, use a placeholder colour image
        if not resolved.get(placeholder):
            color = asset.get("dominant_color", "#cccccc")
            placeholder_bytes = _generate_placeholder(color, bbox)
            uploaded = await _upload_to_files(
                placeholder_bytes, "image/png", app_code, client_code,
                api_client, headers, cache,
            )
            if uploaded:
                resolved[placeholder] = uploaded
            stats["failed"] += 1

    logger.info(
        "Asset resolution: %d total — %d SimpleIcons, %d Clearbit, %d cropped, %d placeholder",
        len(assets), stats["simpleicons"], stats["clearbit"], stats["crop"], stats["failed"],
    )
    return resolved


async def _try_logo_lookup(brand: str) -> str | None:
    """Try SimpleIcons then Clearbit for a brand logo."""
    slug = brand.lower().strip().replace(" ", "").replace("logo", "")
    # SimpleIcons
    url = f"https://cdn.simpleicons.org/{slug}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.head(url, follow_redirects=True)
            if resp.status_code == 200:
                logger.info("SimpleIcons hit for '%s' → %s", brand, url)
                return url
    except Exception:
        pass

    # Clearbit
    domain = f"{slug}.com"
    url = f"https://logo.clearbit.com/{domain}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.head(url, follow_redirects=True)
            if resp.status_code == 200:
                logger.info("Clearbit hit for '%s' → %s", brand, url)
                return url
    except Exception:
        pass

    return None


async def _download_url(url: str) -> bytes | None:
    """Download bytes from a URL."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, follow_redirects=True)
            if resp.status_code == 200:
                return resp.content
    except Exception as e:
        logger.warning("Download failed for %s: %s", url, e)
    return None


def _crop_from_screenshot(
    img: Image.Image,
    bbox: list[int],
    y_offset: int,
) -> bytes | None:
    """Crop a region from the full screenshot."""
    try:
        x, y, bw, bh = bbox
        # Translate to page-level coords
        abs_y = y + y_offset
        # Add padding
        pad = 4
        left = max(0, x - pad)
        top = max(0, abs_y - pad)
        right = min(img.width, x + bw + pad)
        bottom = min(img.height, abs_y + bh + pad)
        if right <= left or bottom <= top:
            return None
        crop = img.crop((left, top, right, bottom))
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        logger.warning("Crop failed: %s", e)
        return None


def _generate_placeholder(color: str, bbox: list[int] | None) -> bytes:
    """Generate a solid-colour placeholder image."""
    w = bbox[2] if bbox and len(bbox) >= 3 else 200
    h = bbox[3] if bbox and len(bbox) >= 4 else 200
    w = max(10, min(w, 800))
    h = max(10, min(h, 800))
    try:
        c = color.lstrip("#")
        rgb = tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        rgb = (204, 204, 204)
    img = Image.new("RGB", (w, h), rgb)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _upload_to_files(
    file_bytes: bytes,
    mime: str,
    app_code: str,
    client_code: str,
    api_client: Any,
    headers: dict[str, str],
    cache: dict[str, str],
) -> str | None:
    """Upload bytes to Modlix files API, with SHA256 dedup cache."""
    sha = hashlib.sha256(file_bytes).hexdigest()[:16]
    if sha in cache:
        return cache[sha]

    ext = "svg" if "svg" in mime else "png" if "png" in mime else "jpg"
    filename = f"asset_{sha}.{ext}"
    path = f"/api/files/static/file/{client_code}/{app_code}/{filename}"

    try:
        # Use httpx directly for multipart upload since SaasClient may not support it
        gateway_url = api_client.base_url if hasattr(api_client, "base_url") else "http://localhost:8080"
        async with httpx.AsyncClient(timeout=30) as client:
            upload_headers = {k: v for k, v in headers.items()}
            resp = await client.post(
                f"{gateway_url}{path}",
                files={"file": (filename, file_bytes, mime)},
                headers=upload_headers,
            )
            if resp.status_code in (200, 201):
                # The response may be the URL or we construct it
                url = f"api/files/static/file/{client_code}/{app_code}/{filename}"
                cache[sha] = url
                logger.info("Uploaded %s (%d bytes) → %s", filename, len(file_bytes), url)
                return url
            else:
                logger.warning("Upload failed %s: HTTP %d — %s", path, resp.status_code, resp.text[:200])
    except Exception as e:
        logger.warning("Upload error for %s: %s", filename, e)
    return None


def rewrite_placeholders(comp_def: dict[str, Any], resolved: dict[str, str]) -> int:
    """Rewrite ASSET_N placeholders in component src properties. Returns count."""
    count = 0
    for comp in comp_def.values():
        props = comp.get("properties", {})
        src = props.get("src", {})
        if isinstance(src, dict):
            val = src.get("value", "")
            if val and val in resolved:
                src["value"] = resolved[val]
                count += 1
    return count
