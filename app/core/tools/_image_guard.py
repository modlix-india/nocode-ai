"""Keep tool-produced images inside what the model will actually accept.

A full-page screenshot of a long marketing page is easily 10,000px tall. The
provider rejects any image whose longer edge exceeds **8192px** with:

    400 .messages[N].image[0]: You have uploaded an unsupported image.
        Please make sure your image is valid and has one of the following
        formats: webp, png, jpeg, and gif.

which reads like a format problem and is actually a size one. Measured against
the live API on 2026-09-16: 1440x8192 is accepted, 1440x8193 is rejected, and
9000x1440 is rejected too, so the cap is per-edge and not about height alone.

Why this matters more than a single failed call: the oversized image goes into
the conversation history, so **every subsequent request in that session fails
too**. One long screenshot kills the chat permanently, which is exactly what
"sessions keep disconnecting" looked like from the outside.

So: downscale anything over the cap, and drop anything that will not decode at
all. A shrunken screenshot is worth far more to the agent than a dead session.
"""

from __future__ import annotations

import base64
import io
import logging

logger = logging.getLogger(__name__)

# Measured provider limit, per edge. Kept a little under 8192 so a rounding
# difference in any re-encode cannot land exactly on the boundary.
MAX_IMAGE_EDGE = 8000

# Second, softer budget. The edge cap alone still lets a 1440x4308 full-page
# screenshot through at 6.2 megapixels, and vision cost scales with pixels, not
# with the longest edge. Capping total area shrinks the tall pages that actually
# cost something while leaving ordinary viewport shots (1440x900 = 1.3MP)
# untouched. 4MP keeps a 1440-wide page at ~1150 wide, where body text is still
# legible to the model -- the point of the screenshot in the first place.
MAX_IMAGE_PIXELS = 4_000_000

# Floor for a resized edge. Applied to the SCALE, never per-axis: clamping one
# axis on its own changes the shape of the image.
_MIN_EDGE = 1

_PNG = "image/png"


def _fit(width: int, height: int) -> tuple[int, int] | None:
    """Target size for an image, or None if it is already small enough.

    ONE scale factor is derived from whichever budget binds hardest and then
    applied to both axes, so the aspect ratio is preserved exactly rather than
    per-axis. Rounding is the only source of drift, and it is bounded by half a
    pixel on each edge.
    """
    if width <= 0 or height <= 0:
        return None

    scale = 1.0
    longest = max(width, height)
    if longest > MAX_IMAGE_EDGE:
        scale = MAX_IMAGE_EDGE / float(longest)

    pixels = float(width) * float(height)
    if pixels * scale * scale > MAX_IMAGE_PIXELS:
        scale = min(scale, (MAX_IMAGE_PIXELS / pixels) ** 0.5)

    if scale >= 1.0:
        return None

    return (
        max(_MIN_EDGE, round(width * scale)),
        max(_MIN_EDGE, round(height * scale)),
    )


def sanitize_image_b64(b64: str, mime: str | None) -> tuple[str, str] | None:
    """Return (base64, mime) safe to send, or None if the image is unusable.

    Passes small images straight through without decoding them, so the common
    case costs nothing.
    """
    if not isinstance(b64, str) or not b64:
        return None

    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:  # noqa: BLE001
        logger.warning("Dropping tool image: not valid base64")
        return None

    if not raw:
        logger.warning("Dropping tool image: empty payload")
        return None

    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a hard dependency
        return b64, (mime or _PNG)

    try:
        with Image.open(io.BytesIO(raw)) as img:
            width, height = img.size
            new_size = _fit(width, height)
            if new_size is None:
                return b64, (mime or _PNG)

            # Convert first: a palette or alpha image can fail to save as PNG
            # after a resize, and RGB is what every provider wants anyway.
            converted = img.convert("RGB").resize(new_size, Image.LANCZOS)
            buf = io.BytesIO()
            converted.save(buf, format="PNG", optimize=True)
    except Image.DecompressionBombError:
        # Far beyond anything our own renderer produces (Pillow's ceiling is
        # ~179 megapixels; a 1440x12000 full-page shot is 17). Decoding it in
        # order to shrink it is the DoS this guard exists to avoid, so drop it.
        logger.warning("Dropping tool image: exceeds Pillow's decompression-bomb limit")
        return None
    except Exception:  # noqa: BLE001
        logger.warning("Dropping tool image: could not decode or resize", exc_info=True)
        return None

    logger.info(
        "Downscaled tool image %dx%d -> %dx%d (edge cap %d, pixel budget %d)",
        width, height, new_size[0], new_size[1], MAX_IMAGE_EDGE, MAX_IMAGE_PIXELS,
    )
    return base64.b64encode(buf.getvalue()).decode(), _PNG
