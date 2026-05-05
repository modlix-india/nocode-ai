"""Screenshot slicer — splits a tall screenshot into section-sized chunks.

Uses pixel-variance row scanning to find natural section boundaries
(solid-colour bands between sections on marketing/landing pages).
Falls back to fixed-height cuts when variance is uniform.
"""

from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

# Allow very tall screenshots (e.g. 1440x27000 = 39M pixels) without PIL warning
Image.MAX_IMAGE_PIXELS = 200_000_000

# Slice height constraints
MIN_SLICE_HEIGHT = 800
MAX_SLICE_HEIGHT = 2400
# Luminance variance threshold — rows below this are candidate cut points.
# Lower = stricter (only truly solid-colour bands). For a 27K-pixel marketing
# page, 80 keeps cuts at real section boundaries without over-splitting.
VARIANCE_THRESHOLD = 80.0


@dataclass
class SliceSpec:
    """One vertical chunk of the screenshot."""
    index: int
    y_start: int
    y_end: int
    height: int
    width: int
    jpeg_b64: str       # JPEG quality 85, base64
    avg_bg_color: str   # hex "#rrggbb"


def slice_screenshot(screenshot_b64: str) -> list[SliceSpec]:
    """Split a tall screenshot into section-sized slices.

    Algorithm:
    1. Compute per-row luminance variance.
    2. Find low-variance bands (natural section boundaries).
    3. Cut at band centres, enforcing min/max height constraints.

    Args:
        screenshot_b64: Base64-encoded PNG or JPEG of the full page.

    Returns:
        List of SliceSpec, each with a JPEG crop and metadata.
    """
    img_bytes = base64.b64decode(screenshot_b64)
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = img.size
    logger.info("Screenshot: %dx%d", w, h)

    if h <= MAX_SLICE_HEIGHT:
        # Short enough to be one slice
        return [_make_slice(img, 0, 0, h, w)]

    arr = np.array(img, dtype=np.float32)
    # Per-row luminance: weighted sum of RGB
    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    # Variance across columns for each row
    row_var = np.var(lum, axis=1)

    # Find candidate cut rows: low variance = solid-colour bands
    candidates = np.nonzero(row_var < VARIANCE_THRESHOLD)[0]
    logger.info("Found %d low-variance candidate rows out of %d", len(candidates), h)

    # Cluster adjacent candidate rows into bands, pick centre of each band
    cut_points = _cluster_to_cuts(candidates, h)
    logger.info("Cut points after clustering: %s", cut_points)

    # Enforce min/max height constraints
    cut_points = _enforce_constraints(cut_points, h, row_var)
    logger.info("Cut points after constraints: %s", cut_points)

    # Build slices
    slices: list[SliceSpec] = []
    prev_y = 0
    for i, y in enumerate(cut_points + [h]):
        if y <= prev_y:
            continue
        slices.append(_make_slice(img, len(slices), prev_y, y, w))
        prev_y = y

    # Merge trailing tiny slice into the previous one
    if len(slices) >= 2 and slices[-1].height < MIN_SLICE_HEIGHT:
        last = slices.pop()
        prev = slices.pop()
        merged = _make_slice(img, prev.index, prev.y_start, last.y_end, w)
        slices.append(merged)

    logger.info("Produced %d slices from %dx%d screenshot", len(slices), w, h)
    return slices


def _cluster_to_cuts(candidates: np.ndarray, total_h: int) -> list[int]:
    """Group adjacent candidate rows into bands, return centre of each."""
    if len(candidates) == 0:
        return []
    cuts = []
    band_start = int(candidates[0])
    prev = band_start
    for row in candidates[1:]:
        row = int(row)
        if row - prev > 5:
            # End of band — cut at centre
            centre = (band_start + prev) // 2
            if centre > 0 and centre < total_h:
                cuts.append(centre)
            band_start = row
        prev = row
    # Last band
    centre = (band_start + prev) // 2
    if centre > 0 and centre < total_h:
        cuts.append(centre)
    return cuts


def _enforce_constraints(cuts: list[int], total_h: int, row_var: np.ndarray) -> list[int]:
    """Enforce min/max slice height constraints."""
    result: list[int] = []
    prev_y = 0

    for cut in sorted(cuts):
        seg_h = cut - prev_y
        if seg_h < MIN_SLICE_HEIGHT:
            # Too small — skip this cut (merge with previous)
            continue
        if seg_h > MAX_SLICE_HEIGHT:
            # Too tall — force-split at the lowest-variance row in the excess
            _force_split(result, prev_y, cut, row_var)
        else:
            result.append(cut)
        prev_y = result[-1] if result else 0

    # Handle trailing segment
    last_y = result[-1] if result else 0
    if total_h - last_y > MAX_SLICE_HEIGHT:
        _force_split(result, last_y, total_h, row_var)

    return result


def _force_split(result: list[int], start: int, end: int, row_var: np.ndarray) -> None:
    """Insert forced cuts into `result` so no segment exceeds MAX_SLICE_HEIGHT."""
    y = start
    while end - y > MAX_SLICE_HEIGHT:
        # Search for lowest-variance row in [y + MIN_SLICE_HEIGHT, y + MAX_SLICE_HEIGHT]
        search_start = y + MIN_SLICE_HEIGHT
        search_end = min(y + MAX_SLICE_HEIGHT, end)
        if search_start >= search_end:
            search_start = y + (end - y) // 2
            search_end = search_start + 1
        segment = row_var[search_start:search_end]
        best_offset = int(np.argmin(segment))
        cut_at = search_start + best_offset
        result.append(cut_at)
        y = cut_at


def _make_slice(img: Image.Image, index: int, y_start: int, y_end: int, width: int) -> SliceSpec:
    """Crop and encode one slice at original resolution."""
    crop = img.crop((0, y_start, width, y_end))

    # Average background colour (sample corners + centre)
    arr = np.array(crop)
    samples = [
        arr[0, 0], arr[0, -1],
        arr[-1, 0], arr[-1, -1],
        arr[arr.shape[0] // 2, arr.shape[1] // 2],
    ]
    avg = np.mean(samples, axis=0).astype(int)
    avg_color = f"#{avg[0]:02x}{avg[1]:02x}{avg[2]:02x}"

    # Encode as JPEG quality 85 at original resolution
    buf = io.BytesIO()
    crop.save(buf, format="JPEG", quality=85)
    jpeg_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    return SliceSpec(
        index=index,
        y_start=y_start,
        y_end=y_end,
        height=y_end - y_start,
        width=width,
        jpeg_b64=jpeg_b64,
        avg_bg_color=avg_color,
    )
