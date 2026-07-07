from __future__ import annotations

"""Image compression utilities for LLM API submissions.

Anthropic's API has a 5MB limit on base64 image payloads.
Screenshots from retina/HiDPI displays are often 5-10MB as PNGs.
This module compresses images to fit within API limits.
"""

import base64
import io
import logging
from PIL import Image

logger = logging.getLogger(__name__)

def _get_limits() -> tuple[int, int]:
    """Get image limits from config settings."""
    from app.config import settings
    max_bytes = int(settings.MAX_IMAGE_BASE64_MB * 1_000_000)
    return max_bytes, settings.IMAGE_MAX_DIMENSION

# JPEG quality steps for progressive compression
QUALITY_STEPS = [85, 70, 55, 40]


def compress_image_base64(
    base64_data: str,
    media_type: str = "image/png",
) -> tuple[str, str]:
    """Compress a base64-encoded image to fit within size limits.

    Strategy:
    1. If already under the limit, return as-is.
    2. Resize to max dimension (1568px) if larger.
    3. Convert PNG/BMP to JPEG (lossy, much smaller).
    4. Progressively reduce JPEG quality until under the limit.

    Args:
        base64_data: Raw base64 string (no data: prefix).
        media_type: MIME type of the input image.

    Returns:
        Tuple of (compressed_base64, new_media_type).
    """
    max_bytes, max_dim = _get_limits()

    # Check if already within limits
    if len(base64_data) <= max_bytes:
        return base64_data, media_type

    original_size = len(base64_data)
    logger.info(
        "Image exceeds %d bytes (%d bytes), compressing...",
        max_bytes,
        original_size,
    )

    try:
        # Decode base64 to PIL Image
        image_bytes = base64.b64decode(base64_data)
        img = Image.open(io.BytesIO(image_bytes))

        # Convert RGBA/palette to RGB (JPEG doesn't support alpha)
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # Step 1: Resize if either dimension exceeds the configured max
        w, h = img.size
        if w > max_dim or h > max_dim:
            ratio = min(max_dim / w, max_dim / h)
            new_w = int(w * ratio)
            new_h = int(h * ratio)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            logger.info("Resized from %dx%d to %dx%d", w, h, new_w, new_h)

        # Step 2: Try JPEG at progressive quality levels
        for quality in QUALITY_STEPS:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            compressed = base64.b64encode(buf.getvalue()).decode("ascii")

            if len(compressed) <= max_bytes:
                reduction = (1 - len(compressed) / original_size) * 100
                logger.info(
                    "Compressed: quality=%d, %d→%d bytes (%.0f%% reduction)",
                    quality,
                    original_size,
                    len(compressed),
                    reduction,
                )
                return compressed, "image/jpeg"

        # Step 3: If still too large, halve dimensions and retry
        w, h = img.size
        img = img.resize((w // 2, h // 2), Image.LANCZOS)
        logger.info("Further resized to %dx%d", w // 2, h // 2)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=60, optimize=True)
        compressed = base64.b64encode(buf.getvalue()).decode("ascii")

        reduction = (1 - len(compressed) / original_size) * 100
        logger.info(
            "Final compression: %d→%d bytes (%.0f%% reduction)",
            original_size,
            len(compressed),
            reduction,
        )
        return compressed, "image/jpeg"

    except Exception as e:
        logger.error("Image compression failed: %s", e)
        # Return original - let the API error handle it
        return base64_data, media_type
