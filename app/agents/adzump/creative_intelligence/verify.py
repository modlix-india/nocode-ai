"""Served-URL verification for creative-library assets (Rules 2/3/6).

A creative's ``fileUrl`` is only trustworthy if the EXACT public path the
browser will use returns renderable bytes - a local file existing proves
nothing (path/permission mistakes are exactly what breaks in the UI). Every
write to CompetitorCreativeLibrary goes through ``verify_creative`` first;
the Rule-9 repair sweep (scripts/sweep_creative_library.py) re-runs the same
checks over the stored records.

Pass criteria (ALL must hold):
  - HTTP 200 after redirects; a redirect that lands on HTML is a FAIL
  - Content-Type image/* or video/* (html/json/xml rejected regardless of 200)
  - body >= MIN_ASSET_BYTES (rejects 0-byte and 1x1 placeholder responses)
  - image: decodes, width > 0 and height > 0
  - video: mp4/webm container magic; ffprobe duration > 0 when ffprobe is
    installed (without it the container sniff is the gate and duration stays 0)
  - not a known CDN placeholder (md5 blacklist - seed hashes as they are met)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

MIN_ASSET_BYTES = 2 * 1024
_TIMEOUT_S = 20.0
_MAX_VERIFY_BYTES = 60 * 1024 * 1024

# Drop reasons (Rule 10 counter keys) - keep in step with the sweep report.
FETCH_FAILED = "fetch_failed"
BAD_CONTENT_TYPE = "bad_content_type"
TOO_SMALL = "too_small"
UNDECODABLE = "undecodable"
ATTRIBUTION_MISMATCH = "attribution_mismatch"
EMPTY_FILE_URL = "empty_file_url"

# md5 of known CDN "image not available" placeholder bodies: a 200 serving one
# of these is a broken asset, not a pass. Seed new hashes from sweep reports.
_KNOWN_PLACEHOLDER_MD5S: frozenset[str] = frozenset()

_VIDEO_MAGIC_WEBM = b"\x1aE\xdf\xa3"


@dataclass
class VerifiedMedia:
    ok: bool
    reason: str = ""            # one of the drop reasons above when not ok
    width: int = 0
    height: int = 0
    duration_seconds: float = 0.0
    content_type: str = ""
    size: int = 0


def _served_url(path: str) -> str:
    """Absolute URL for a stored /api/files/... path - the same base the
    uploads went through, so the check exercises the real serving stack."""
    if path.startswith(("http://", "https://")):
        return path
    return settings.GATEWAY_URL.rstrip("/") + "/" + path.lstrip("/")


async def verify_served_asset(path: str, *, expect: str) -> VerifiedMedia:
    """GET ``path`` through the public serving stack and validate it as
    ``expect`` ("image" | "video"). Never raises."""
    if not (path or "").strip():
        return VerifiedMedia(ok=False, reason=EMPTY_FILE_URL)
    url = _served_url(path)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S,
                                     follow_redirects=True) as client:
            resp = await client.get(url)
    except Exception as e:
        logger.info("verify_fetch_failed: url=%s err=%s", url[:200], str(e)[:120])
        return VerifiedMedia(ok=False, reason=FETCH_FAILED)
    if resp.status_code != 200:
        return VerifiedMedia(ok=False, reason=FETCH_FAILED)

    ctype = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if not (ctype.startswith("image/") or ctype.startswith("video/")):
        # html error pages, json bodies, xml - regardless of the 200.
        return VerifiedMedia(ok=False, reason=BAD_CONTENT_TYPE)

    data = resp.content[:_MAX_VERIFY_BYTES]
    if len(data) < MIN_ASSET_BYTES:
        return VerifiedMedia(ok=False, reason=TOO_SMALL)
    if hashlib.md5(data).hexdigest() in _KNOWN_PLACEHOLDER_MD5S:
        return VerifiedMedia(ok=False, reason=UNDECODABLE)

    if expect == "image":
        return _validate_image(data, ctype)
    return await _validate_video(data, ctype, url)


def _validate_image(data: bytes, ctype: str) -> VerifiedMedia:
    try:
        from PIL import Image

        with Image.open(BytesIO(data)) as img:
            width, height = img.size
    except Exception:
        return VerifiedMedia(ok=False, reason=UNDECODABLE)
    if width <= 1 or height <= 1:  # 1x1 tracking/placeholder pixel
        return VerifiedMedia(ok=False, reason=TOO_SMALL)
    return VerifiedMedia(ok=True, width=width, height=height,
                         content_type=ctype, size=len(data))


async def _validate_video(data: bytes, ctype: str, url: str) -> VerifiedMedia:
    is_mp4 = b"ftyp" in data[:16]
    is_webm = data.startswith(_VIDEO_MAGIC_WEBM)
    if not (is_mp4 or is_webm):
        return VerifiedMedia(ok=False, reason=UNDECODABLE)
    duration = await _ffprobe_duration(url)
    if duration is not None and duration <= 0:
        return VerifiedMedia(ok=False, reason=UNDECODABLE)
    return VerifiedMedia(ok=True, duration_seconds=duration or 0.0,
                         content_type=ctype, size=len(data))


async def _ffprobe_duration(url: str) -> float | None:
    """Video duration via ffprobe, or None when ffprobe isn't installed /
    errors out (the container sniff stays the gate then)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
            "-of", "csv=p=0", url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_S)
        return float(out.decode().strip())
    except FileNotFoundError:
        logger.info("verify_ffprobe_missing: container sniff is the video gate")
        return None
    except Exception:
        return None


async def verify_creative(creative) -> tuple[bool, str]:
    """Verify one ``Creative`` end-to-end and stamp the Rule-6 facts on it.

    Returns (keep, drop_reason). A video whose poster fails verification keeps
    the creative but CLEARS poster_url - a verified fileUrl must never travel
    with an unverified posterUrl (Rule 8)."""
    if not (creative.file_url or "").strip():
        return False, EMPTY_FILE_URL

    expect = "video" if creative.media_type == "video" else "image"
    media = await verify_served_asset(creative.file_url, expect=expect)
    if not media.ok:
        return False, media.reason

    creative.width = media.width
    creative.height = media.height
    if media.width and media.height:
        creative.aspect_ratio = round(media.width / media.height, 3)
    creative.duration_seconds = round(media.duration_seconds, 2)

    if creative.media_type == "video" and creative.poster_url:
        poster = await verify_served_asset(creative.poster_url, expect="image")
        if poster.ok:
            creative.poster_width = poster.width
            creative.poster_height = poster.height
        else:
            logger.info("verify_poster_dropped: id=%s reason=%s",
                        creative.creative_id, poster.reason)
            creative.poster_url = ""
            creative.poster_width = creative.poster_height = 0

    creative.verified_at = datetime.now(timezone.utc).isoformat()
    return True, ""
