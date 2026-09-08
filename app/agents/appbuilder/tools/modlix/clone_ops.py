"""Clone-loop primitives: harvest source assets + compare source vs build.

Two tools, both designed for the external-site clone scenarios:

  extract_site_assets(url)
      Drives Playwright across the source page, collects every <img>, inline
      <svg>, and CSS background-image URL, fetches the bytes, and uploads each
      one to the active app's Modlix static-asset space under
      `<app>/global/clone/<sha8>.<ext>`. Returns a manifest mapping
      `original_url → modlix_url`. Use the modlix_url verbatim when binding
      Image components — never invent placeholder URLs and never generate AI
      imagery for content photos when cloning.

  compare_to_source(page_name, source_handle, region?)
      Looks up the source screenshot cached under `source_handle` (populated
      by screenshot_external_url), screenshots the just-built Modlix page,
      and asks the active LLM (vision-capable provider required) to produce a
      structured JSON diff. Returns
      `[{section, severity, copy_diff, layout_diff, color_diff,
        missing_elements, fix_suggestion}, ...]` for the agent to act on.

The compare loop is what "exact clone" requires: each region the agent builds
gets verified against the source pixels before the next region starts. Both
tools share the screenshot_external_url cache via session.context.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json as _json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

from .visuals import (
    _MIME_PNG,
    _resolve_app_code,
    _resolve_client_code,
    _gateway_url,
)


# ── shared helpers ──────────────────────────────────────────────────────


_MIME_BY_EXT: dict[str, str] = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "svg": "image/svg+xml", "webp": "image/webp",
    "ico": "image/x-icon", "avif": "image/avif",
}


def _ext_for_mime(mime: str) -> str:
    mime = (mime or "").lower().strip()
    if mime.startswith("image/svg"):
        return "svg"
    if mime == "image/jpeg":
        return "jpg"
    if mime == "image/png":
        return "png"
    if mime == "image/webp":
        return "webp"
    if mime == "image/gif":
        return "gif"
    if mime == "image/x-icon" or mime == "image/vnd.microsoft.icon":
        return "ico"
    if mime == "image/avif":
        return "avif"
    return "bin"


def _short_sha8(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:10]


async def _upload_bytes_as_static(
    *,
    ac: str,
    cc: str,
    headers: dict[str, str],
    payload: bytes,
    filename: str,
    mime: str,
    page_name: str = "global",
    folder: str = "clone",
) -> tuple[bool, str | None, str | None]:
    """Upload raw bytes to the static asset space; return (ok, public_url, err)."""
    from app.config import settings
    path_parts = [ac, page_name.strip("/") or "global"]
    if folder:
        path_parts.append(folder.strip("/"))
    path_segment = "/" + "/".join(path_parts)
    upload_path = f"/{ac}/{cc}/page/api/files/static{path_segment}"

    req_headers = dict(headers or {})
    req_headers["clientCode"] = cc
    req_headers.pop("Content-Type", None)
    url = _gateway_url() + upload_path
    query: dict[str, Any] = {"clientCode": cc, "override": "true"}
    try:
        async with httpx.AsyncClient(timeout=getattr(settings, "HTTP_TIMEOUT", 30.0)) as client:
            files = {"file": (filename, payload, mime or "application/octet-stream")}
            resp = await client.post(url, headers=req_headers, files=files, params=query)
    except Exception as e:  # noqa: BLE001
        return False, None, f"{type(e).__name__}: {e}"
    if resp.status_code >= 400:
        return False, None, f"HTTP {resp.status_code}: {resp.text[:240]}"
    dl_path = f"/api/files/static/file/{cc}{path_segment}/{filename}"
    public_url = _gateway_url() + dl_path
    return True, public_url, None


async def _ensure_style_doc(
    ac: str, cc: str, headers: dict[str, str], name: str, css: str,
) -> tuple[bool, str | None]:
    """Create (or update if it exists) a global-CSS style doc. Idempotent so a
    re-run of a clone doesn't 409. Mirrors the /api/ui/styles contract used by
    the create_style / update_style tools."""
    from app.config import settings
    base = _gateway_url() + "/api/ui/styles"
    req_headers = dict(headers or {})
    req_headers["clientCode"] = cc
    req_headers["appCode"] = ac
    req_headers.setdefault("Content-Type", "application/json")
    try:
        async with httpx.AsyncClient(timeout=getattr(settings, "HTTP_TIMEOUT", 30.0)) as client:
            existing = None
            try:
                gr = await client.get(base, headers=req_headers, params={"appCode": ac, "name": name, "size": 50})
                if gr.status_code < 400:
                    for s in (gr.json() or {}).get("content", []) or []:
                        if s.get("name") == name:
                            existing = s
                            break
            except Exception:  # noqa: BLE001
                pass
            if existing:
                existing["styleString"] = css
                existing["message"] = "Clone fonts via CFA"
                pr = await client.put(f"{base}/{existing.get('id')}", headers=req_headers, json=existing)
                return (pr.status_code < 400), (None if pr.status_code < 400 else f"PUT {pr.status_code}: {pr.text[:200]}")
            body = {"name": name, "appCode": ac, "clientCode": cc, "styleString": css, "message": "Clone fonts via CFA"}
            pr = await client.post(base, headers=req_headers, json=body)
            return (pr.status_code < 400), (None if pr.status_code < 400 else f"POST {pr.status_code}: {pr.text[:200]}")
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


# ═════════════════════════════════════════════════════════════════════════
#  extract_site_assets
# ═════════════════════════════════════════════════════════════════════════


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
    if (xml.length > 80_000) continue;  // skip absurdly large inline svgs
    const r = el.getBoundingClientRect();
    out.svgs.push({
      index: svgIdx,
      xml: xml,
      width: Math.round(r.width),
      height: Math.round(r.height),
      role: el.closest('header,nav') ? 'header'
            : el.closest('footer') ? 'footer'
            : 'content',
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
        url: u,
        width: Math.round(r.width),
        height: Math.round(r.height),
        role: el === document.body ? 'body-bg' : 'bg',
      });
    }
  }
  // Fonts: the page's primary font-family + every loaded font-file URL, so a
  // clone can reproduce typography instead of falling back to a system font.
  try {
    const bodyCS = getComputedStyle(document.body);
    const fontFiles = [];
    for (const e of performance.getEntriesByType('resource')) {
      if (/\.(woff2?|ttf|otf)(\?|$)/i.test(e.name)) {
        const u = abs(e.name);
        if (u && !fontFiles.includes(u)) fontFiles.push(u);
      }
    }
    out.fonts = { family: bodyCS.fontFamily || '', files: fontFiles.slice(0, 8) };
  } catch (_) { out.fonts = { family: '', files: [] }; }
  return out;
}
"""


async def _execute_extract_site_assets(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    url = (params.get("url") or "").strip()
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return ToolResult(success=False, error="`url` must be an absolute http(s) URL")

    # Per-session cache: extracting assets is slow (headless render + N uploads)
    # and agents tend to re-call it. If this URL was already harvested in this
    # session, return the cached manifest instantly instead of re-rendering.
    session_context = context.get("session_context") if isinstance(context, dict) else None
    assets_cache = (
        session_context.setdefault("_clone_assets", {})
        if isinstance(session_context, dict) else None
    )
    if assets_cache is not None and url in assets_cache:
        cached = assets_cache[url]
        n = len(cached.get("originals") or [])
        return ToolResult(
            success=True,
            summary=(
                f"[cached] {url} was already extracted this session ({n} asset(s) uploaded). "
                "Reusing the manifest — NOT re-rendering. Bind the `modlix_url` values from "
                "result.data.originals. Do NOT call extract_site_assets again for this URL."
            ),
            data=cached,
        )

    max_assets = max(1, min(int(params.get("max_assets") or 50), 200))
    wait_ms = max(500, min(int(params.get("wait_ms") or 2500), 30000))
    viewport_width = max(320, min(int(params.get("viewport_width") or 1440), 3840))

    ac, err = _resolve_app_code(params, context)
    if err:
        return err
    cc = _resolve_client_code(params, context)
    headers = dict(context.get("headers") or {})

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return ToolResult(success=False, error="playwright not installed; pip install playwright && python -m playwright install chromium")

    harvest: dict[str, list[dict[str, Any]]] = {"imgs": [], "svgs": [], "bgs": []}
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            ctx_b = await browser.new_context(
                viewport={"width": viewport_width, "height": 900},
                ignore_https_errors=True,
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            )
            page = await ctx_b.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(wait_ms)
            # Scroll the whole page so lazy <img> elements load before harvesting.
            try:
                await page.evaluate(
                    "() => new Promise((res) => {"
                    " let y = 0;"
                    " const step = () => {"
                    "   window.scrollTo(0, y);"
                    "   y += 600;"
                    "   if (y < document.documentElement.scrollHeight) setTimeout(step, 60);"
                    "   else { window.scrollTo(0, 0); setTimeout(res, 400); }"
                    " }; step();"
                    "})"
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                harvest = await page.evaluate(_HARVEST_JS)
            except Exception as e:  # noqa: BLE001
                await browser.close()
                return ToolResult(success=False, error=f"harvest JS failed: {type(e).__name__}: {e}")
            await browser.close()
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"render error: {type(e).__name__}: {e}")

    items: list[dict[str, Any]] = []
    # IMG + BG entries share the same fetch+upload path.
    for kind, entry in (
        *[("img", e) for e in harvest.get("imgs") or []],
        *[("bg",  e) for e in harvest.get("bgs")  or []],
    ):
        items.append({"kind": kind, **entry})
    # Inline SVGs go via their serialized XML — no HTTP fetch needed.
    for entry in harvest.get("svgs") or []:
        items.append({"kind": "svg", **entry})

    items = items[:max_assets]
    if not items:
        return ToolResult(success=True, summary=f"No assets harvested from {url}.", data={"originals": []})

    originals: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    seen_sha: dict[str, str] = {}

    async def _process_one(item: dict[str, Any]) -> dict[str, Any] | None:
        try:
            if item["kind"] == "svg":
                payload = item["xml"].encode("utf-8")
                mime = "image/svg+xml"
                base_name = f"svg{item['index']:03d}"
                origin = f"<inline-svg #{item['index']}>"
            else:
                src = item["url"]
                async with httpx.AsyncClient(
                    timeout=30.0,
                    follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 ModlixCloneBot/1.0"},
                ) as client:
                    resp = await client.get(src)
                if resp.status_code >= 400:
                    return {"src": src, "error": f"HTTP {resp.status_code}"}
                payload = resp.content
                mime = (resp.headers.get("content-type") or "").split(";")[0].strip().lower() or "application/octet-stream"
                if not mime.startswith("image/"):
                    return {"src": src, "error": f"non-image content-type: {mime}"}
                base_name = "img"
                origin = src
            sha = _short_sha8(payload)
            if sha in seen_sha:
                return {"src": origin, "modlix_url": seen_sha[sha], "skipped": "dedup-by-content"}
            ext = _ext_for_mime(mime)
            filename = f"{base_name}_{sha}.{ext}"
            ok, public_url, up_err = await _upload_bytes_as_static(
                ac=ac, cc=cc, headers=headers, payload=payload,
                filename=filename, mime=mime, page_name="global", folder="clone",
            )
            if not ok:
                return {"src": origin, "error": up_err or "upload failed"}
            seen_sha[sha] = public_url
            return {
                "src": origin,
                "kind": item["kind"],
                "modlix_url": public_url,
                "mime": mime,
                "bytes": len(payload),
                "sha8": sha,
                "width": item.get("width"),
                "height": item.get("height"),
                "role": item.get("role"),
                "alt": item.get("alt"),
            }
        except Exception as e:  # noqa: BLE001
            return {"src": item.get("url") or f"<svg#{item.get('index')}>", "error": f"{type(e).__name__}: {e}"}

    sem = asyncio.Semaphore(8)

    async def _bounded(item: dict[str, Any]) -> dict[str, Any] | None:
        async with sem:
            return await _process_one(item)

    results = await asyncio.gather(*(_bounded(i) for i in items))
    for r in results:
        if not r:
            continue
        if "error" in r:
            failures.append(r)
        else:
            originals.append(r)

    # Compact summary; full manifest in data.
    lines = [
        f"Extracted {len(originals)} asset(s) from {url} (failures: {len(failures)}, skipped-dedup: "
        f"{sum(1 for o in originals if o.get('skipped'))}).",
        "Bind these `modlix_url` values into Image components — never invent placeholder URLs.",
    ]
    for o in originals[:12]:
        lines.append(
            f"  {o.get('role','?'):>8}  {o.get('width') or '?'}x{o.get('height') or '?'}  "
            f"{o['modlix_url']}  (src: {o['src'][:70]})"
        )
    if len(originals) > 12:
        lines.append(f"  ... +{len(originals)-12} more (see result.data.originals)")
    if failures:
        lines.append("Failures (truncated):")
        for f in failures[:5]:
            lines.append(f"  {f.get('src','?')[:80]}  → {f.get('error','?')[:80]}")

    # ---- Fonts: download the source's web fonts, host them, and create a
    # global @font-face style doc so the clone reproduces typography instead of
    # falling back to a system font. Best-effort: any failure never blocks the
    # asset manifest (the tool's primary job).
    font_info: dict[str, Any] = {}
    _GENERIC = {"", "monospace", "sans-serif", "serif", "system-ui", "ui-monospace", "ui-sans-serif", "cursive", "fantasy", "inherit", "initial"}
    try:
        fonts = harvest.get("fonts") or {}
        family_raw = (fonts.get("family") or "").strip()
        first = family_raw.split(",")[0].strip().strip('"').strip("'") if family_raw else ""
        primary = first if first.lower() not in _GENERIC else ""
        uploaded: list[dict[str, Any]] = []
        for furl in [f for f in (fonts.get("files") or []) if isinstance(f, str)][:8]:
            try:
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 ModlixCloneBot/1.0"}) as fc:
                    fr = await fc.get(furl)
                if fr.status_code >= 400 or not fr.content:
                    continue
                fname = (furl.split("/")[-1].split("?")[0]) or "font.ttf"
                fmime = (fr.headers.get("content-type") or "").split(";")[0].strip().lower() or "font/ttf"
                ok_u, public_url, _u_err = await _upload_bytes_as_static(
                    ac=ac, cc=cc, headers=headers, payload=fr.content,
                    filename=fname, mime=fmime, page_name="global", folder="fonts",
                )
                if ok_u and public_url:
                    rel = public_url
                    gw = _gateway_url()
                    if rel.startswith(gw):
                        rel = rel[len(gw):]  # root-relative so it works on any host
                    uploaded.append({"url": rel, "filename": fname})
            except Exception:  # noqa: BLE001
                continue
        if uploaded and primary:
            faces = []
            for uf in uploaded:
                low = uf["filename"].lower()
                fmt = "woff2" if low.endswith(".woff2") else "woff" if low.endswith(".woff") else "opentype" if low.endswith(".otf") else "truetype"
                faces.append(
                    f"@font-face{{font-family:'{primary}';src:url('{uf['url']}') format('{fmt}');"
                    f"font-weight:100 900;font-style:{'italic' if 'italic' in low else 'normal'};font-display:swap;}}"
                )
            global_rule = f"*{{font-family:{family_raw or repr(primary)} !important;}}"
            css = "\n".join(faces) + "\n" + global_rule
            ok_s, s_err = await _ensure_style_doc(ac, cc, headers, "cloneFonts", css)
            font_info = {"family": primary, "files": [u["url"] for u in uploaded], "style_doc": "cloneFonts" if ok_s else None}
            if ok_s:
                lines.append(f"Fonts: hosted {len(uploaded)} font file(s) and applied '{primary}' app-wide via the 'cloneFonts' style doc. Do NOT set per-component fontFamily — the global rule handles it.")
            else:
                font_info["error"] = s_err
                lines.append(f"Fonts: hosted {len(uploaded)} file(s) but the style doc failed ({s_err}); call create_style(name='cloneFonts', ...) with the @font-face CSS to finish.")
        elif fonts.get("files"):
            font_info = {"note": "source fonts found but primary family was generic/undetected; left as-is", "family_raw": family_raw}
    except Exception as e:  # noqa: BLE001
        font_info = {"error": f"{type(e).__name__}: {e}"}

    result_data = {"url": url, "originals": originals, "failures": failures, "fonts": font_info}
    if assets_cache is not None:
        assets_cache[url] = result_data
    return ToolResult(
        success=True,
        summary="\n".join(lines),
        data=result_data,
    )


extract_site_assets_tool = ToolDefinition(
    name="extract_site_assets",
    description=(
        "Harvest every <img>, inline <svg>, and CSS background-image from an "
        "external page; fetch the bytes; upload each to the active app's "
        "static-asset space; return a manifest. ALSO harvests the page's web "
        "FONTS: it downloads the source's font files, hosts them, and creates a "
        "global 'cloneFonts' @font-face style doc that applies the real font "
        "family app-wide (result.data.fonts reports what was done). Because of "
        "this, do NOT set per-component fontFamily on a clone and do NOT worry "
        "about typography — calling this once handles it. Use this BEFORE "
        "authoring a clone: bind the returned modlix_url values straight into "
        "Image components. Never invent placeholder URLs and never generate AI "
        "imagery for content photos when cloning."
    ),
    parameters=[
        ToolParameter(name="url", type="string", description="Absolute http(s) URL of the page to harvest"),
        ToolParameter(name="max_assets", type="integer", required=False, default=50, description="Cap on total assets (1-200)"),
        ToolParameter(name="viewport_width", type="integer", required=False, default=1440, description="Render viewport width in CSS px"),
        ToolParameter(name="wait_ms", type="integer", required=False, default=2500, description="Wait after load before harvesting (ms)"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to the app this session is working in"),
        ToolParameter(name="client_code", type="string", required=False, description="clientCode; defaults to session"),
    ],
    execute=_execute_extract_site_assets,
)


# ═════════════════════════════════════════════════════════════════════════
#  compare_to_source
# ═════════════════════════════════════════════════════════════════════════


_COMPARE_PROMPT = (
    "You will be shown two screenshots: the SOURCE site (first image) and the "
    "current MODLIX BUILD (second image). The build is meant to be an exact "
    "clone of the source.\n\n"
    "Return a JSON array of diff entries. Each entry MUST have exactly these "
    "keys: section, severity, copy_diff, layout_diff, color_diff, "
    "missing_elements, fix_suggestion.\n\n"
    "- `section`: short name of the visual region (e.g. 'hero', 'feature-cards', "
    "'logo-strip', 'footer').\n"
    "- `severity`: one of 'high' (must fix before clone is acceptable), "
    "'medium' (visible but not critical), 'low' (cosmetic / can ship).\n"
    "- `copy_diff`: any wrong/missing/extra text. Quote the SOURCE text "
    "verbatim and the BUILD text verbatim. Empty string when copy matches.\n"
    "- `layout_diff`: structural differences (wrong order, overlapping "
    "elements, missing rows/columns, wrong alignment). One concrete sentence.\n"
    "- `color_diff`: palette/contrast differences. Empty string when colours "
    "match.\n"
    "- `missing_elements`: array of source elements absent from the build "
    "(empty array when nothing is missing). Use short labels.\n"
    "- `fix_suggestion`: one actionable sentence telling the build agent what "
    "to change. Reference the Modlix tool to use when obvious "
    "(add_component / patch_component_styles / replace_page_definition).\n\n"
    "Be strict. If the build looks roughly like the source but text is wrong, "
    "that is `high` severity. Sections out of visual order are `high`. "
    "Missing imagery is `high`. Wrong font weight or 5% color drift is "
    "`medium`. Slight spacing differences are `low`.\n\n"
    "Reply with ONLY the JSON array — no prose, no markdown fences."
)


async def _screenshot_modlix_page(
    *,
    page_name: str,
    ac: str,
    cc: str,
    width: int,
    height: int,
    wait_ms: int,
    headers: dict[str, str],
) -> tuple[bytes | None, str | None]:
    """Render a Modlix page and return (png_bytes, error). Uses anonymous=True."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None, "playwright not installed"

    from app.config import settings
    host = getattr(settings, "PREVIEW_HOST", "") or settings.GATEWAY_URL
    host = host.rstrip("/")
    target_url = f"{host}/{ac}/{cc}/page/{page_name}"

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            ctx_b = await browser.new_context(
                viewport={"width": width, "height": height},
                ignore_https_errors=True,
            )
            page = await ctx_b.new_page()
            try:
                await page.goto(target_url, wait_until="networkidle", timeout=30000)
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(wait_ms)
            png = await page.screenshot(full_page=True, type="png")
            await browser.close()
            return png, None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def _safe_parse_json_array(raw: str) -> tuple[list[dict[str, Any]] | None, str | None]:
    if not raw:
        return None, "empty response"
    text = raw.strip()
    # Strip code fences if the model snuck them in despite instructions
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # Find the first '[' and last ']' to be tolerant of stray prose
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None, "no JSON array delimiters in response"
    candidate = text[start:end + 1]
    try:
        parsed = _json.loads(candidate)
    except Exception as e:  # noqa: BLE001
        return None, f"JSON parse error: {e}"
    if not isinstance(parsed, list):
        return None, "top-level JSON value is not an array"
    return [x for x in parsed if isinstance(x, dict)], None


async def _execute_compare_to_source(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    source_handle = (params.get("source_handle") or "").strip()
    if not page_name:
        return ToolResult(success=False, error="`page_name` is required")
    if not source_handle:
        return ToolResult(success=False, error="`source_handle` is required (returned by screenshot_external_url)")

    ac, err = _resolve_app_code(params, context)
    if err:
        return err
    cc = _resolve_client_code(params, context)
    headers = dict(context.get("headers") or {})
    width = max(320, min(int(params.get("width") or 1440), 3840))
    height = max(320, min(int(params.get("height") or 900), 2160))
    wait_ms = max(500, min(int(params.get("wait_ms") or 2500), 30000))

    session_context = context.get("session_context") if isinstance(context, dict) else None
    if not isinstance(session_context, dict):
        return ToolResult(success=False, error="No session context — compare_to_source must run in an agent session.")
    cache = session_context.get("_clone_source_shots") or {}
    src = cache.get(source_handle)
    if not src:
        # Provide hint with the cached handles available so the agent can recover.
        known = list(cache.keys())[:8]
        hint = f"Known handles: {known}" if known else "No source handles cached — call screenshot_external_url first."
        return ToolResult(success=False, error=f"Unknown source_handle '{source_handle}'. {hint}")
    src_b64 = src.get("image_base64")
    src_mime = src.get("image_mime") or _MIME_PNG
    if not src_b64:
        return ToolResult(success=False, error=f"Cached source for '{source_handle}' has no image bytes.")

    # Render the Modlix build.
    build_png, ss_err = await _screenshot_modlix_page(
        page_name=page_name, ac=ac, cc=cc,
        width=width, height=height, wait_ms=wait_ms, headers=headers,
    )
    if ss_err:
        return ToolResult(success=False, error=f"build screenshot failed: {ss_err}")
    assert build_png is not None
    build_b64 = base64.b64encode(build_png).decode("ascii")

    # Call the provider (Anthropic-only for now; OpenAI vision would need a
    # different message shape and we're locked to anthropic here).
    try:
        from app.services.llm_provider import get_llm_provider
        from app.config import settings
        provider_name = (getattr(settings, "APPBUILDER_PROVIDER", "") or "anthropic").lower()
        if provider_name != "anthropic":
            return ToolResult(success=False, error=f"compare_to_source requires APPBUILDER_PROVIDER=anthropic; current={provider_name}")
        provider = get_llm_provider("anthropic")
        model = provider.get_model(getattr(settings, "AGENT_MODEL_TIER", "balanced"))
        import anthropic  # type: ignore[import-not-found]
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        msg = await asyncio.to_thread(
            client.messages.create,
            model=model,
            max_tokens=4096,
            system="You produce strict JSON diff arrays for site-clone QA. Reply with ONLY the JSON array.",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "SOURCE (target to clone):"},
                    {"type": "image", "source": {"type": "base64", "media_type": src_mime, "data": src_b64}},
                    {"type": "text", "text": "MODLIX BUILD (current state):"},
                    {"type": "image", "source": {"type": "base64", "media_type": _MIME_PNG, "data": build_b64}},
                    {"type": "text", "text": _COMPARE_PROMPT},
                ],
            }],
        )
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"compare LLM call failed: {type(e).__name__}: {e}")

    raw_text = ""
    for block in (msg.content or []):
        if getattr(block, "type", "") == "text":
            raw_text += getattr(block, "text", "")
    diffs, parse_err = _safe_parse_json_array(raw_text)
    if diffs is None:
        return ToolResult(
            success=False,
            error=f"could not parse diff JSON: {parse_err}\n--- raw response ---\n{raw_text[:1200]}",
        )

    # Sort by severity (high first) and build a compact text summary.
    rank = {"high": 0, "medium": 1, "low": 2}
    diffs_sorted = sorted(diffs, key=lambda d: rank.get(str(d.get("severity", "")).lower(), 3))
    sev_counts: dict[str, int] = {}
    for d in diffs_sorted:
        sev = str(d.get("severity", "")).lower() or "unknown"
        sev_counts[sev] = sev_counts.get(sev, 0) + 1

    lines = [
        f"compare_to_source({page_name}) vs {source_handle} — {len(diffs_sorted)} diff(s).",
        "Severity counts: " + ", ".join(f"{k}={v}" for k, v in sev_counts.items()),
        "",
    ]
    for d in diffs_sorted[:15]:
        sev = str(d.get("severity", "?")).upper()
        section = d.get("section", "?")
        fix = d.get("fix_suggestion", "")
        layout = d.get("layout_diff", "")
        copy = d.get("copy_diff", "")
        miss = d.get("missing_elements") or []
        lines.append(f"[{sev}] {section}")
        if layout:
            lines.append(f"  layout:  {layout}")
        if copy:
            lines.append(f"  copy:    {copy}")
        if miss:
            lines.append(f"  missing: {miss}")
        if fix:
            lines.append(f"  fix:     {fix}")
    if len(diffs_sorted) > 15:
        lines.append(f"... +{len(diffs_sorted)-15} more (see result.data.diffs)")

    # Cache the build screenshot under a build_handle so the next compare round
    # can be wired up by the agent if it wants to keep references.
    build_handle = f"build:{page_name}:{_short_sha8(build_png)}"
    builds_cache = session_context.setdefault("_clone_build_shots", {})
    if isinstance(builds_cache, dict):
        if len(builds_cache) >= 24:
            try:
                _oldest = next(iter(builds_cache))
                builds_cache.pop(_oldest, None)
            except StopIteration:
                pass
        builds_cache[build_handle] = {
            "page_name": page_name,
            "image_base64": build_b64,
            "image_mime": _MIME_PNG,
        }

    return ToolResult(
        success=True,
        summary="\n".join(lines),
        data={
            "page_name": page_name,
            "source_handle": source_handle,
            "build_handle": build_handle,
            "severity_counts": sev_counts,
            "diffs": diffs_sorted,
            # Also attach the build screenshot so the agent can SEE it natively.
            "image_base64": build_b64,
            "image_mime": _MIME_PNG,
        },
    )


compare_to_source_tool = ToolDefinition(
    name="compare_to_source",
    description=(
        "Compare the just-built Modlix page against the cached source "
        "screenshot. Returns a JSON array of structured diffs "
        "(section/severity/copy_diff/layout_diff/color_diff/missing_elements/"
        "fix_suggestion). Call after each region you build. Iterate fixes "
        "until all `severity=high` diffs are gone before moving to the next "
        "section. Requires APPBUILDER_PROVIDER=anthropic."
    ),
    parameters=[
        ToolParameter(name="page_name", type="string", description="Modlix page to render and compare"),
        ToolParameter(name="source_handle", type="string", description="Source screenshot handle (returned by screenshot_external_url)"),
        ToolParameter(name="width", type="integer", required=False, default=1440, description="Modlix render viewport width (CSS px)"),
        ToolParameter(name="height", type="integer", required=False, default=900, description="Modlix render viewport height (CSS px)"),
        ToolParameter(name="wait_ms", type="integer", required=False, default=2500, description="Wait after page load before snapping (ms)"),
        ToolParameter(name="app_code", type="string", required=False, description="appCode; defaults to the app this session is working in"),
        ToolParameter(name="client_code", type="string", required=False, description="clientCode; defaults to session"),
    ],
    execute=_execute_compare_to_source,
)


TOOLS: list[ToolDefinition] = [
    extract_site_assets_tool,
    compare_to_source_tool,
]
