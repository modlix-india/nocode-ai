"""Image downloading and format normalization utilities."""

from __future__ import annotations

import base64
import logging
from io import BytesIO
from typing import Any
from PIL import Image

from app.agents.adzump._shared import emit_progress

logger = logging.getLogger(__name__)


_LOGO_CACHE: dict[str, tuple[str, str]] = {}


async def download_and_normalize_logo(
    logo_url: str | None, client: Any, headers: dict, context: dict
) -> tuple[str, str]:
    """Download logo and convert to PNG.

    Returns:
        tuple: (logo_b64, mime_type)
    """
    if not logo_url:
        raise ValueError("Brand logo is missing. Please upload a logo first.")

    if logo_url in _LOGO_CACHE:
        logger.info("Logo cache hit for URL: %s", logo_url)
        return _LOGO_CACHE[logo_url]

    await emit_progress(context, "Downloading brand logo...")
    logo_res = await client.get(logo_url, headers=headers)
    if not logo_res.success or not logo_res.data:
        raise ValueError(f"Failed to download brand logo from {logo_url}")

    try:
        logo_img = Image.open(BytesIO(logo_res.data))
        logo_buf = BytesIO()
        logo_img.save(logo_buf, format="PNG")
        logo_b64 = base64.b64encode(logo_buf.getvalue()).decode("utf-8")
        result = (logo_b64, "image/png")
        _LOGO_CACHE[logo_url] = result
        return result
    except Exception as e:
        raise ValueError(
            "Vector logo formats (like SVGs) are not supported. Please upload a PNG or JPEG logo."
        ) from e


async def get_base_image_b64(
    path: str, client: Any, headers: dict
) -> tuple[str, str] | None:
    """Download a base image and return normalized base64 JPEG content."""
    try:
        res = await client.get(path, headers=headers)
        if res.success and res.data:
            img = Image.open(BytesIO(res.data))
            if img.mode == "RGBA":
                img = img.convert("RGB")
            buf = BytesIO()
            img.save(buf, format="JPEG")
            return base64.b64encode(buf.getvalue()).decode("utf-8"), "image/jpeg"
    except Exception as e:
        logger.warning("Failed to process base image from %s: %s", path, e)
    return None
