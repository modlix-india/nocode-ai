"""`screenshot_external_url` — capture an external page at multiple scroll
positions and attach each PNG as an image content block.

v4's clone scenarios need the agent to see the source site directly. The
PNGs are forwarded to the LLM as image blocks (via BaseAgent's existing
image-extraction path) so the agent reads pixels, not paraphrases. Each
shot is also cached under a stable `source_handle` in a PROCESS-LEVEL
dict keyed by session_id so `compare_to_source` can fetch the same image
later. We do NOT cache in `session.context` — that gets persisted to
MySQL and base64 PNGs blow its column size limit.

No Gemini, no auto-describe. v4 runs on Anthropic; the agent reads images
natively.
"""

from __future__ import annotations

import asyncio
import base64
import re
from typing import Any
from urllib.parse import urlparse

from app.agents.appbuilderv4.tools._shot_cache import get_shot_cache
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult


_MIME_PNG = "image/png"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _handle_slug(url: str) -> str:
    u = urlparse(url)
    host = re.sub(r"[^a-z0-9]+", "-", (u.netloc or "host").lower()).strip("-") or "host"
    path = re.sub(r"[^a-z0-9]+", "-", (u.path or "/").lower()).strip("-") or "root"
    return f"{host}__{path}"


async def _execute_screenshot_external_url(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    url = (params.get("url") or "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return ToolResult(success=False, error="`url` must be an absolute http(s) URL")
    raw_positions = params.get("scroll_positions") or [0.0, 0.5, 1.0]
    if not isinstance(raw_positions, list):
        return ToolResult(success=False, error="`scroll_positions` must be a list of fractions 0.0-1.0")
    positions = [max(0.0, min(1.0, float(p))) for p in raw_positions if isinstance(p, (int, float))]
    if not positions:
        return ToolResult(success=False, error="`scroll_positions` had no valid numeric entries")
    width = max(320, min(int(params.get("viewport_width") or 1440), 3840))
    height = max(320, min(int(params.get("viewport_height") or 900), 2160))
    wait_ms = max(200, min(int(params.get("wait_ms") or 2500), 30000))

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return ToolResult(success=False, error="playwright not installed")

    url_slug = _handle_slug(url)
    # Use the process-level cache (not session.context) so base64 PNGs
    # don't get persisted into the MySQL CONTEXT_JSON column on each turn.
    session_id = ""
    sc = context.get("session_context") if isinstance(context, dict) else None
    if isinstance(sc, dict):
        session_id = str(sc.get("session_id") or sc.get("_session_id") or "")
    if not session_id:
        # Fall back to a stable per-process key for the rare standalone case.
        session_id = "_unattached_"

    shots: list[dict[str, Any]] = []
    text_parts: list[str] = [
        f"Captured {len(positions)} screenshot(s) of {url} @ {width}x{height}.",
        "Each PNG is attached as an image content block — look at it directly.",
        "Pass the `source_handle` of any shot to `compare_to_source` later.",
    ]

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            ctx_b = await browser.new_context(
                viewport={"width": width, "height": height},
                ignore_https_errors=True,
                user_agent=_USER_AGENT,
            )
            page = await ctx_b.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception:  # noqa: BLE001
                pass  # ad-heavy sites never settle; continue
            await page.wait_for_timeout(wait_ms)
            doc_h = await page.evaluate("document.documentElement.scrollHeight")
            view_h = height
            for pos in positions:
                y = int(pos * max(0, doc_h - view_h))
                await page.evaluate(f"window.scrollTo({{top: {y}, behavior: 'instant'}})")
                await page.wait_for_timeout(400)
                png = await page.screenshot(full_page=False, type="png")
                encoded = base64.b64encode(png).decode("ascii")
                label = f"w{width}_y{int(pos * 100):03d}"
                handle = f"{url_slug}:{label}"
                shot = {
                    "label": label,
                    "source_handle": handle,
                    "scroll_fraction": pos,
                    "viewport_width": width,
                    "doc_height": doc_h,
                    "image_base64": encoded,
                    "image_mime": _MIME_PNG,
                }
                shots.append(shot)
                from app.agents.appbuilderv4.tools._shot_cache import put_shot
                put_shot(session_id, handle, {
                    "url": url, "viewport_width": width, "scroll_fraction": pos,
                    "image_base64": encoded, "image_mime": _MIME_PNG,
                })
                text_parts.append(f"  {label}  handle={handle}  (scroll={int(pos*100)}%)")
            await ctx_b.close()
            await browser.close()
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"render error: {type(e).__name__}: {e}")

    return ToolResult(
        success=True,
        summary="\n".join(text_parts),
        data={"url": url, "shots": shots},
    )


screenshot_external_url_tool = ToolDefinition(
    name="screenshot_external_url",
    description=(
        "Visit an external (non-Modlix) URL in headless Chromium and capture "
        "PNG screenshots at the given scroll positions. Each PNG is attached "
        "as an image content block — your next turn sees the pixels directly. "
        "Each shot also gets a stable `source_handle` (e.g. 'linear-app__root:w1440_y000') "
        "you can later pass to `compare_to_source` to diff your build against "
        "the same source frame.\n\n"
        "Use this BEFORE composing a clone in code_run. Cache the handles — "
        "do NOT call this tool a second time on the same URL unless the "
        "source itself changed."
    ),
    parameters=[
        ToolParameter(name="url", type="string", description="Absolute http(s) URL to capture"),
        ToolParameter(name="scroll_positions", type="array", required=False, default=[0.0, 0.5, 1.0],
                      description="Fractions of doc height (0.0=top, 1.0=bottom).",
                      items={"type": "number"}),
        ToolParameter(name="viewport_width", type="integer", required=False, default=1440,
                      description="Render viewport width in CSS px (320-3840)."),
        ToolParameter(name="viewport_height", type="integer", required=False, default=900,
                      description="Render viewport height in CSS px (320-2160)."),
        ToolParameter(name="wait_ms", type="integer", required=False, default=2500,
                      description="Milliseconds to wait after load before the first capture."),
    ],
    execute=_execute_screenshot_external_url,
)
