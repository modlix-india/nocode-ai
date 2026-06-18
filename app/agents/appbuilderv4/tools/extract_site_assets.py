"""`extract_site_assets` — harvest every <img>, inline <svg>, and CSS
background-image from an external page, upload each to Modlix files
under the current app, and return a manifest mapping original → modlix_url.

Use BEFORE composing imagery on a clone. Bind the returned modlix_url
values into Image components — never invent placeholder URLs and never
generate AI imagery for content photos when cloning.

Lifts the v3 approach from app/agents/appbuilder/tools/modlix/clone_ops.py
with two simplifications:
  - The v4 SDK is unavailable here (we're a regular tool, not running
    inside code_run), so we hit the gateway directly via httpx with the
    same headers the rest of the v4 tool context carries.
  - No idempotency cache — every call uploads fresh. Re-runs of the same
    URL produce new uploads under different sha8-named files; that's an
    acceptable cost for the size of typical clone scenarios.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult


_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_HARVEST_JS = r"""
() => {
  const out = {imgs: [], svgs: [], bgs: []};
  const seen = new Set();
  const abs = (u) => { try { return new URL(u, location.href).href; } catch (_) { return null; } };
  for (const el of document.querySelectorAll('img')) {
    const src = abs(el.currentSrc || el.src);
    if (!src || seen.has(src)) continue;
    seen.add(src);
    const r = el.getBoundingClientRect();
    out.imgs.push({
      url: src,
      alt: (el.alt || '').slice(0, 240),
      width: Math.round(r.width),
      height: Math.round(r.height),
      role: el.closest('header,nav') ? 'header'
            : el.closest('footer') ? 'footer'
            : (r.width > 600 ? 'hero' : 'content'),
    });
  }
  let svgIdx = 0;
  for (const el of document.querySelectorAll('svg')) {
    svgIdx += 1;
    const xml = new XMLSerializer().serializeToString(el);
    if (xml.length > 80_000) continue;
    const r = el.getBoundingClientRect();
    out.svgs.push({
      index: svgIdx, xml,
      width: Math.round(r.width), height: Math.round(r.height),
      role: el.closest('header,nav') ? 'header'
            : el.closest('footer') ? 'footer' : 'content',
    });
  }
  for (const el of document.querySelectorAll('*')) {
    const style = getComputedStyle(el);
    const bg = style.backgroundImage;
    if (!bg || bg === 'none') continue;
    const matches = bg.matchAll(/url\((['\"]?)([^'\")]+)\1\)/g);
    for (const m of matches) {
      const u = abs(m[2]);
      if (!u || seen.has(u)) continue;
      seen.add(u);
      const r = el.getBoundingClientRect();
      out.bgs.push({
        url: u, width: Math.round(r.width), height: Math.round(r.height),
        role: el === document.body ? 'body-bg' : 'bg',
      });
    }
  }
  return out;
}
"""


_EXT_FOR_MIME: dict[str, str] = {
    "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
    "image/svg+xml": "svg", "image/webp": "webp", "image/avif": "avif",
    "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico",
}


def _ext_for_mime(mime: str) -> str:
    return _EXT_FOR_MIME.get((mime or "").lower().split(";")[0].strip(), "bin")


def _sha8(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()[:10]


async def _upload_static(client: httpx.AsyncClient, gateway_url: str, headers: dict[str, str],
                          ac: str, cc: str, payload: bytes, filename: str, mime: str) -> tuple[bool, str | None, str | None]:
    """POST raw bytes to /<app>/<client>/page/api/files/static/<app>/global/clone/<filename>.
    Returns (ok, public_url, error_string)."""
    path_segment = f"/{ac}/global/clone"
    upload_path = f"/{ac}/{cc}/page/api/files/static{path_segment}"
    req_headers = dict(headers)
    req_headers["clientCode"] = cc
    req_headers.pop("Content-Type", None)
    url = gateway_url + upload_path
    try:
        files = {"file": (filename, payload, mime or "application/octet-stream")}
        resp = await client.post(url, headers=req_headers, files=files,
                                 params={"clientCode": cc, "override": "true"}, timeout=30.0)
    except Exception as e:  # noqa: BLE001
        return False, None, f"{type(e).__name__}: {e}"
    if resp.status_code >= 400:
        return False, None, f"HTTP {resp.status_code}: {resp.text[:200]}"
    dl_path = f"/api/files/static/file/{cc}{path_segment}/{filename}"
    return True, gateway_url + dl_path, None


async def _execute_extract_site_assets(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    url = (params.get("url") or "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return ToolResult(success=False, error="`url` must be absolute http(s)")
    max_assets = max(1, min(int(params.get("max_assets") or 50), 200))
    viewport_width = max(320, min(int(params.get("viewport_width") or 1440), 3840))
    wait_ms = max(500, min(int(params.get("wait_ms") or 2500), 30000))

    ac = (params.get("app_code") or context.get("app_code") or "").strip()
    cc = (params.get("client_code") or context.get("client_code") or "").strip()
    if not ac:
        return ToolResult(success=False, error="No app_code in context")
    headers = dict(context.get("headers") or {})

    from app.config import settings
    gateway_url = (getattr(settings, "GATEWAY_URL", "") or "").rstrip("/")
    if not gateway_url:
        return ToolResult(success=False, error="GATEWAY_URL not configured")

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return ToolResult(success=False, error="playwright not installed")

    harvest: dict[str, list[dict]] = {"imgs": [], "svgs": [], "bgs": []}
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            ctx_b = await browser.new_context(
                viewport={"width": viewport_width, "height": 900},
                ignore_https_errors=True, user_agent=_USER_AGENT,
            )
            page = await ctx_b.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(wait_ms)
            # Trigger lazy-loaded imagery by scrolling top to bottom.
            try:
                await page.evaluate(
                    "() => new Promise((res) => { let y = 0;"
                    " const step = () => {"
                    "  window.scrollTo(0, y); y += 600;"
                    "  if (y < document.documentElement.scrollHeight) setTimeout(step, 60);"
                    "  else { window.scrollTo(0, 0); setTimeout(res, 400); }"
                    " }; step(); })"
                )
            except Exception:  # noqa: BLE001
                pass
            harvest = await page.evaluate(_HARVEST_JS)
            await browser.close()
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"render error: {type(e).__name__}: {e}")

    items: list[dict[str, Any]] = []
    items += [{"kind": "img", **e} for e in (harvest.get("imgs") or [])]
    items += [{"kind": "bg",  **e} for e in (harvest.get("bgs")  or [])]
    items += [{"kind": "svg", **e} for e in (harvest.get("svgs") or [])]
    items = items[:max_assets]
    if not items:
        return ToolResult(success=True, summary=f"No assets harvested from {url}.",
                          data={"originals": []})

    seen_sha: dict[str, str] = {}
    originals: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(8)

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True,
                                  headers={"User-Agent": _USER_AGENT}) as fetch_client, \
               httpx.AsyncClient(timeout=30.0) as upload_client:

        async def _process(item: dict[str, Any]) -> dict[str, Any] | None:
            async with sem:
                try:
                    if item["kind"] == "svg":
                        payload = item["xml"].encode("utf-8")
                        mime = "image/svg+xml"
                        base_name = f"svg{item['index']:03d}"
                        origin = f"<inline-svg #{item['index']}>"
                    else:
                        r = await fetch_client.get(item["url"])
                        if r.status_code >= 400:
                            return {"src": item["url"], "error": f"HTTP {r.status_code}"}
                        payload = r.content
                        mime = (r.headers.get("content-type") or "").split(";")[0].strip().lower() or "application/octet-stream"
                        if not mime.startswith("image/"):
                            return {"src": item["url"], "error": f"non-image: {mime}"}
                        base_name = "img"
                        origin = item["url"]
                    sha = _sha8(payload)
                    if sha in seen_sha:
                        return {"src": origin, "modlix_url": seen_sha[sha], "skipped": "dedup"}
                    filename = f"{base_name}_{sha}.{_ext_for_mime(mime)}"
                    ok, modlix_url, err = await _upload_static(
                        upload_client, gateway_url, headers, ac, cc, payload, filename, mime,
                    )
                    if not ok:
                        return {"src": origin, "error": err or "upload failed"}
                    seen_sha[sha] = modlix_url
                    return {"src": origin, "kind": item["kind"], "modlix_url": modlix_url,
                            "mime": mime, "bytes": len(payload), "sha8": sha,
                            "width": item.get("width"), "height": item.get("height"),
                            "role": item.get("role"), "alt": item.get("alt")}
                except Exception as e:  # noqa: BLE001
                    return {"src": item.get("url") or "?", "error": f"{type(e).__name__}: {e}"}

        results = await asyncio.gather(*(_process(i) for i in items))

    for r in results:
        if not r:
            continue
        if "error" in r:
            failures.append(r)
        else:
            originals.append(r)

    lines = [
        f"Harvested {len(originals)} asset(s) from {url} "
        f"(failures={len(failures)}, dedup={sum(1 for o in originals if o.get('skipped'))}).",
        "Bind these `modlix_url` values into Image components — never invent URLs.",
    ]
    for o in originals[:14]:
        lines.append(
            f"  {o.get('role','?'):>8}  {o.get('width') or '?'}x{o.get('height') or '?'}  "
            f"{o['modlix_url']}  ← {o['src'][:60]}"
        )
    if len(originals) > 14:
        lines.append(f"  ... +{len(originals)-14} more (see result.data.originals)")
    if failures:
        lines.append("First failures:")
        for f in failures[:5]:
            lines.append(f"  {f.get('src','?')[:60]}  → {f.get('error','?')[:80]}")

    return ToolResult(success=True, summary="\n".join(lines),
                      data={"url": url, "originals": originals, "failures": failures})


extract_site_assets_tool = ToolDefinition(
    name="extract_site_assets",
    description=(
        "Harvest every <img>, inline <svg>, and CSS background-image from an "
        "external page; download the bytes; upload each to the current app's "
        "static-asset space under `<app>/global/clone/<sha>.<ext>`; return a "
        "manifest of `{src, modlix_url, mime, width, height, role, alt}`. "
        "Use this BEFORE authoring imagery on a clone — bind the returned "
        "`modlix_url` values into Image components. NEVER invent URLs and "
        "NEVER use generate_image for content photos when cloning."
    ),
    parameters=[
        ToolParameter(name="url", type="string", description="Absolute http(s) URL"),
        ToolParameter(name="max_assets", type="integer", required=False, default=50,
                      description="Cap on harvested assets (1-200)"),
        ToolParameter(name="viewport_width", type="integer", required=False, default=1440,
                      description="Render width in CSS px (320-3840)"),
        ToolParameter(name="wait_ms", type="integer", required=False, default=2500,
                      description="Wait after load before harvesting (ms)"),
        ToolParameter(name="app_code", type="string", required=False,
                      description="Override target app code (defaults to session)"),
        ToolParameter(name="client_code", type="string", required=False,
                      description="Override target client code (defaults to session)"),
    ],
    execute=_execute_extract_site_assets,
)
