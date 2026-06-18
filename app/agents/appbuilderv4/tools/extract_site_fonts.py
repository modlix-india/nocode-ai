"""`extract_site_fonts` — harvest web-font URLs from an external page,
download the font files, upload them under the current Modlix app, and
return a manifest the agent can wire into `app.properties.fontPacks`.

Why: clones look "close" without the right font and "exact" with it.
Linear uses Inter Display (custom CDN .woff2 files); Stripe uses Stripe
Sans; etc. Without loading the source's fonts, every typography metric
drifts and `compare_to_source` flags it.

How the agent USES the manifest:
  1. extract_site_fonts(url) → returns
     {fonts: [{family, weight, style, src_url, modlix_url, format}]}
  2. Group by family. Build a fontPacks dict:
       {family_name: [{src, weight, style, format}, ...]}
  3. PUT /api/ui/applications/{app_code}  with app.properties.fontPacks
     set to that dict. The platform serves the fonts; @font-face is
     auto-registered on page load.
  4. Components then reference `font-family: '<family_name>'` in
     styleProperties — and the browser actually loads the right font.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any
from urllib.parse import urljoin

import httpx

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult


_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# JS that walks document.fonts + parses @font-face from all <style> /
# stylesheet rules. Returns a deduped list of {family, weight, style,
# src_url, format}.
_HARVEST_FONTS_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const push = (fam, weight, style, url, fmt) => {
    const key = `${fam}|${weight}|${style}|${url}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({family: fam, weight, style, src_url: url, format: fmt});
  };

  // 1. @font-face rules from stylesheets
  for (const sheet of Array.from(document.styleSheets)) {
    let rules;
    try { rules = sheet.cssRules || []; }
    catch (_) { continue; }  // cross-origin sheets throw
    for (const r of Array.from(rules)) {
      if (r.type !== 5 /* CSSFontFaceRule */) continue;
      const fam = (r.style.fontFamily || '').replace(/['"]/g, '').trim();
      const weight = (r.style.fontWeight || '400').trim();
      const style = (r.style.fontStyle || 'normal').trim();
      const srcRaw = r.style.src || '';
      // src can have multiple url('...') entries with format hints.
      const m = srcRaw.matchAll(/url\((['"]?)([^'")]+)\1\)(?:\s*format\((['"]?)([^'")]+)\3\))?/g);
      for (const item of m) {
        const u = (() => { try { return new URL(item[2], location.href).href; } catch (_) { return null; } })();
        if (!u) continue;
        const fmt = (item[4] || '').toLowerCase();
        push(fam, weight, style, u, fmt);
      }
    }
  }

  // 2. document.fonts as a fallback (covers JS-injected webfonts)
  if (document.fonts && document.fonts.values) {
    for (const f of Array.from(document.fonts.values())) {
      const fam = (f.family || '').replace(/['"]/g, '').trim();
      const weight = (f.weight || '400').trim();
      const style = (f.style || 'normal').trim();
      // document.fonts entries don't expose src; we already grabbed urls
      // from @font-face above. Only emit a stub when no url known.
      const dummy = `font-loaded:${fam}:${weight}:${style}`;
      if (![...seen].some(k => k.startsWith(`${fam}|${weight}|${style}|`))) {
        push(fam, weight, style, dummy, '');
      }
    }
  }

  return out;
};
"""


_EXT_FOR_FORMAT: dict[str, str] = {
    "woff2": "woff2", "woff": "woff",
    "truetype": "ttf", "opentype": "otf", "embedded-opentype": "eot",
}


def _ext_from_url(url: str) -> str:
    m = re.search(r"\.(woff2|woff|ttf|otf|eot)(?:\?|$)", url.lower())
    return m.group(1) if m else "woff2"


def _sha8(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:10]


async def _upload(client: httpx.AsyncClient, gateway: str, headers: dict[str, str],
                   ac: str, cc: str, payload: bytes, filename: str) -> tuple[bool, str | None, str | None]:
    """Multipart upload to /api/files/static under <app>/global/fonts/."""
    upload_path = f"/{ac}/{cc}/page/api/files/static/{ac}/global/fonts"
    req_headers = dict(headers)
    req_headers["clientCode"] = cc
    req_headers.pop("Content-Type", None)
    try:
        files = {"file": (filename, payload, "application/octet-stream")}
        resp = await client.post(gateway + upload_path, headers=req_headers, files=files,
                                  params={"clientCode": cc, "override": "true"}, timeout=30.0)
    except Exception as e:  # noqa: BLE001
        return False, None, f"{type(e).__name__}: {e}"
    if resp.status_code >= 400:
        return False, None, f"HTTP {resp.status_code}: {resp.text[:200]}"
    public = f"/api/files/static/file/{cc}/{ac}/global/fonts/{filename}"
    return True, gateway + public, None


async def _execute_extract_site_fonts(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    url = (params.get("url") or "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return ToolResult(success=False, error="`url` must be absolute http(s)")
    wait_ms = max(500, min(int(params.get("wait_ms") or 3000), 30000))
    max_fonts = max(1, min(int(params.get("max_fonts") or 24), 60))

    ac = (params.get("app_code") or context.get("app_code") or "").strip()
    cc = (params.get("client_code") or context.get("client_code") or "").strip()
    if not ac:
        return ToolResult(success=False, error="No app_code in context")
    headers = dict(context.get("headers") or {})

    from app.config import settings
    gateway = (getattr(settings, "GATEWAY_URL", "") or "").rstrip("/")
    if not gateway:
        return ToolResult(success=False, error="GATEWAY_URL not configured")

    # 1) Render the page in Playwright and harvest @font-face rules + document.fonts.
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return ToolResult(success=False, error="playwright not installed")

    raw: list[dict[str, Any]] = []
    # Sites like linear.app inject @font-face via JS or load fonts from
    # cross-origin stylesheets the scanner can't read. The network listener
    # catches every font/* response regardless. We dedupe these against
    # JS-extracted entries by URL.
    network_fonts: list[dict[str, Any]] = []
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return ToolResult(success=False, error="playwright not installed")
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            ctx_b = await browser.new_context(
                viewport={"width": 1440, "height": 900}, ignore_https_errors=True,
                user_agent=_USER_AGENT,
            )
            page = await ctx_b.new_page()

            def _on_response(resp):  # noqa: ANN001
                try:
                    ct = (resp.headers.get("content-type") or "").lower()
                    u = resp.url
                    if (ct.startswith("font/")
                            or "application/font" in ct
                            or "application/vnd.ms-fontobject" in ct
                            or re.search(r"\.(woff2|woff|ttf|otf|eot)(?:\?|$)", u.lower())):
                        network_fonts.append({"url": u, "content_type": ct})
                except Exception:  # noqa: BLE001
                    pass

            page.on("response", _on_response)

            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(wait_ms)
            # Force-trigger lazy font loads by exercising the DOM a bit.
            try:
                await page.evaluate("document.body.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1500)
            except Exception:  # noqa: BLE001
                pass
            try:
                raw = await page.evaluate(_HARVEST_FONTS_JS)
            except Exception as e:  # noqa: BLE001
                await browser.close()
                return ToolResult(success=False, error=f"harvest JS failed: {type(e).__name__}: {e}")
            await browser.close()
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"render error: {type(e).__name__}: {e}")

    # Merge network-captured fonts that aren't already in `raw`. Network
    # entries have no family/weight/style metadata — derive them from
    # filenames when possible (e.g. "Inter-Regular.woff2" → Inter/400/normal).
    seen_urls = {f.get("src_url") for f in raw}
    name_re = re.compile(r"([A-Za-z][A-Za-z0-9_-]+?)[-_](Thin|ExtraLight|Light|Regular|Medium|SemiBold|Bold|ExtraBold|Black|Variable|\d{3})(Italic)?", re.IGNORECASE)
    weight_map = {
        "thin": "100", "extralight": "200", "light": "300", "regular": "400",
        "medium": "500", "semibold": "600", "bold": "700", "extrabold": "800",
        "black": "900",
    }
    for nf in network_fonts:
        u = nf["url"]
        if u in seen_urls:
            continue
        seen_urls.add(u)
        fname = u.rsplit("/", 1)[-1].split("?")[0]
        m = name_re.search(fname)
        if m:
            fam_raw = m.group(1)
            weight_raw = m.group(2)
            italic = bool(m.group(3))
            family = re.sub(r"([a-z])([A-Z])", r"\1 \2", fam_raw)  # CamelCase → words
            weight = weight_map.get(weight_raw.lower(), weight_raw if weight_raw.isdigit() else "400")
            style = "italic" if italic else "normal"
        else:
            family = re.sub(r"\.(woff2?|ttf|otf|eot).*$", "", fname, flags=re.I).replace("-", " ")
            weight = "400"
            style = "normal"
        raw.append({
            "family": family, "weight": weight, "style": style,
            "src_url": u, "format": _ext_from_url(u),
        })

    # Filter to entries with real URLs (drop "font-loaded:..." stubs); cap.
    fonts = [f for f in raw if isinstance(f.get("src_url"), str)
             and f["src_url"].startswith(("http://", "https://"))][:max_fonts]
    if not fonts:
        # Surface what we saw, even stubs, so the agent knows.
        return ToolResult(success=True,
                          summary=f"No downloadable @font-face URLs found at {url}. "
                                  f"document.fonts saw {len(raw)} entries but none had a src URL "
                                  "(likely JS-injected blobs).",
                          data={"fonts": [], "raw": raw[:20]})

    # 2) Download + upload each font file.
    out: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    seen_sha: dict[str, str] = {}
    sem = asyncio.Semaphore(6)

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True,
                                  headers={"User-Agent": _USER_AGENT}) as fetch, \
               httpx.AsyncClient(timeout=30.0) as upload_client:

        async def process(f: dict[str, Any]) -> dict[str, Any] | None:
            async with sem:
                try:
                    r = await fetch.get(f["src_url"])
                    if r.status_code >= 400:
                        return {"src_url": f["src_url"], "error": f"HTTP {r.status_code}"}
                    payload = r.content
                    sha = _sha8(payload)
                    if sha in seen_sha:
                        return {**f, "modlix_url": seen_sha[sha], "skipped": "dedup"}
                    ext = _EXT_FOR_FORMAT.get(f.get("format", ""), _ext_from_url(f["src_url"]))
                    fam_slug = re.sub(r"[^a-zA-Z0-9]+", "_", f["family"]).strip("_") or "font"
                    weight = f.get("weight") or "400"
                    style = f.get("style") or "normal"
                    filename = f"{fam_slug}_{weight}_{style}_{sha}.{ext}"
                    ok, modlix_url, err = await _upload(upload_client, gateway, headers,
                                                        ac, cc, payload, filename)
                    if not ok:
                        return {**f, "error": err or "upload failed"}
                    seen_sha[sha] = modlix_url
                    return {**f, "modlix_url": modlix_url, "bytes": len(payload), "sha8": sha}
                except Exception as e:  # noqa: BLE001
                    return {**f, "error": f"{type(e).__name__}: {e}"}

        results = await asyncio.gather(*(process(f) for f in fonts))
    for r in results:
        if not r:
            continue
        if "error" in r:
            failures.append(r)
        else:
            out.append(r)

    # 3) Build a fontPacks-shaped suggestion the agent can paste verbatim
    #    into `app.properties.fontPacks`. The platform's runtime
    #    (src/App/App.tsx:processFontPacks) expects a UUID-keyed map of
    #    `{name, code}` where `code` is literal HTML (a <style> or <link>
    #    block) injected into the page head. Each family becomes ONE
    #    entry with all its @font-face declarations inside a single
    #    <style> block.
    import uuid as _uuid_lib
    by_family: dict[str, list[dict[str, Any]]] = {}
    for f in out:
        if f.get("skipped"):
            continue
        by_family.setdefault(f["family"], []).append(f)

    pack: dict[str, dict[str, str]] = {}
    for family, faces in by_family.items():
        # Collapse multiple weights/styles for one family into one <style> block.
        rules: list[str] = []
        for face in faces:
            fmt = face.get("format") or _ext_from_url(face["modlix_url"])
            rules.append(
                f"@font-face{{font-family:'{family}';"
                f"src:url('{face['modlix_url']}') format('{fmt}');"
                f"font-weight:{face.get('weight','400')};"
                f"font-style:{face.get('style','normal')};"
                f"font-display:swap;}}"
            )
        pack[str(_uuid_lib.uuid4())] = {
            "name": family,
            "code": f"<style>{''.join(rules)}</style>",
        }

    lines = [
        f"Extracted {len(out)} font file(s) from {url} (failures={len(failures)}).",
        f"{len(pack)} font famil{'ies' if len(pack)!=1 else 'y'}: {list(pack)}",
        "",
        "To install on the app, PUT /api/ui/applications/<app> with "
        "app.properties.fontPacks set to (paste verbatim):",
        "",
    ]
    import json as _json
    lines.append(_json.dumps(pack, indent=2)[:2400])
    if failures:
        lines.append("")
        lines.append(f"Failures (first 3 of {len(failures)}):")
        for f in failures[:3]:
            lines.append(f"  {f.get('src_url','?')[:60]}  → {f.get('error','?')[:80]}")
    return ToolResult(
        success=True,
        summary="\n".join(lines),
        data={"url": url, "fonts": out, "failures": failures,
              "fontPacks_suggested": pack},
    )


extract_site_fonts_tool = ToolDefinition(
    name="extract_site_fonts",
    description=(
        "Harvest @font-face web-font URLs from an external page, download "
        "every .woff2/.woff/.ttf file, upload to the current Modlix app's "
        "static asset space under `<app>/global/fonts/`, and return a "
        "`fontPacks`-shaped manifest you can PUT verbatim into "
        "`app.properties.fontPacks` to make the platform serve them. "
        "After registering fontPacks, components reference the font via "
        "`font-family: '<family-name>'` in styleProperties.\n\n"
        "Call this BEFORE styling typography on a clone — without it the "
        "browser falls back to a system font and your typography never "
        "matches the source."
    ),
    parameters=[
        ToolParameter(name="url", type="string", description="Absolute http(s) URL of the source page"),
        ToolParameter(name="max_fonts", type="integer", required=False, default=24,
                      description="Cap on font files (1-60)"),
        ToolParameter(name="wait_ms", type="integer", required=False, default=3000,
                      description="Wait after page load before harvesting (ms)"),
        ToolParameter(name="app_code", type="string", required=False,
                      description="Override target app code (defaults to session)"),
        ToolParameter(name="client_code", type="string", required=False,
                      description="Override target client code (defaults to session)"),
    ],
    execute=_execute_extract_site_fonts,
)
