"""`extract_site_assets` — unified Playwright recon for external sites.

ONE call does everything the agent needs to start a clone:

  1. Visits the URL in headless Chromium at multiple viewport widths
     (desktop 1440 / tablet 768 / mobile 375 by default) in ONE browser.
  2. Injects a DOM-walking JS (`_RECON_JS`) per viewport that returns
     `{sections, hovers, animations, videos, font_stack}`.
  3. Captures a FULL-PAGE PNG per viewport plus PER-SECTION viewport-
     aligned PNGs. For every hover trigger surfaced by the recon, drives
     the hover and screenshots the revealed UI.
  4. Harvests every `<img>` / inline `<svg>` / CSS `background-image` /
     `<video>`/ `<source>` / `<picture>` element on the desktop visit and
     uploads each to `/{app}/{client}/page/api/files/static/{app}/global/clone/`.
  5. Harvests web fonts via dual-path (CSS `@font-face` walk + network
     response listener attached for the whole session — catches Cloudflare-
     served fonts the CSS walker misses). Uploads under `global/fonts/`.
     Builds a `fontPacks_suggested` dict ready for `app.properties.fontPacks`.
  6. Caches every screenshot in `_shot_cache` keyed by the session and a
     stable `source_handle` (e.g. `linear-app__root:section_hero_w1440`).
     `compare_to_source` reads from the same cache by handle.

Replaces three prior tools: `screenshot_external_url`, `extract_site_fonts`,
and the old asset-only `extract_site_assets`. Single Playwright launch per
call (was 3).

Emits `progress("...")` lines via `context["progress"]` — those flow to the
chat UI as `tool_update` SSE events so the user sees what's happening
during the ~30-60s recon.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import uuid as _uuid_lib
from typing import Any
from urllib.parse import urlparse

import httpx

from app.agents.appbuilderv4.tools._shot_cache import put_shot
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult


_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_MIME_PNG = "image/png"
_MIME_OCTET = "application/octet-stream"
_ERR_UPLOAD_FAILED = "upload failed"


_EXT_FOR_MIME: dict[str, str] = {
    "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
    "image/svg+xml": "svg", "image/webp": "webp", "image/avif": "avif",
    "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico",
    "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov",
    "video/ogg": "ogv",
}

_EXT_FOR_FONT_FORMAT: dict[str, str] = {
    "woff2": "woff2", "woff": "woff",
    "truetype": "ttf", "opentype": "otf", "embedded-opentype": "eot",
}


def _ext_for_mime(mime: str) -> str:
    return _EXT_FOR_MIME.get((mime or "").lower().split(";")[0].strip(), "bin")


def _ext_for_font_url(url: str) -> str:
    m = re.search(r"\.(woff2|woff|ttf|otf|eot)(?:\?|$)", url.lower())
    return m.group(1) if m else "woff2"


def _sha8(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()[:10]


def _handle_slug(url: str) -> str:
    u = urlparse(url)
    host = re.sub(r"[^a-z0-9]+", "-", (u.netloc or "host").lower()).strip("-") or "host"
    path = re.sub(r"[^a-z0-9]+", "-", (u.path or "/").lower()).strip("-") or "root"
    return f"{host}__{path}"


# DOM-walking script. Runs ENTIRELY in the browser via page.evaluate().
# Returns `{sections, hovers, animations, videos, font_stack}`.
# Capped for performance: 20 sections, 20 hovers, 40 animations, scan
# limits on element walks.
_RECON_JS = r"""
() => {
  const out = {sections: [], hovers: [], animations: [], videos: [], font_stack: {}};
  const kebab = (s) => (s || '').toLowerCase()
    .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) || null;
  const domPath = (el) => {
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur !== document.body && parts.length < 6) {
      let part = cur.tagName.toLowerCase();
      if (cur.id) { parts.unshift(part + '#' + cur.id); break; }
      const sibs = Array.from((cur.parentElement || {}).children || [])
                       .filter(x => x.tagName === cur.tagName);
      if (sibs.length > 1) part += ':nth-of-type(' + (sibs.indexOf(cur) + 1) + ')';
      parts.unshift(part);
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  };
  const isHidden = (s) => s.display === 'none' || s.visibility === 'hidden'
    || parseFloat(s.opacity || '1') === 0;

  // --- sections: top-level page regions ---
  // First pass: direct children of <main>. Works for sites with explicit
  // <section> siblings. Falls back to a heading-driven walk when the page
  // wraps everything in a single container (linear.app etc).
  const main = document.querySelector('main') || document.body;
  let tops = Array.from(main.children).filter(el => {
    if (!el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.height >= 80 && !isHidden(s);
  });
  if (tops.length <= 1) {
    // Heading-driven fallback: every h1/h2 belongs to a section. Walk up
    // from each heading until we find a container ≥ 200px tall that we
    // haven't already used.
    const heads = main.querySelectorAll('h1, h2');
    const containers = [];
    const used = new Set();
    for (const h of heads) {
      let cur = h.parentElement;
      while (cur && cur !== main && cur !== document.body) {
        const r = cur.getBoundingClientRect();
        if (r.height >= 200 && !used.has(cur)) break;
        cur = cur.parentElement;
      }
      if (cur && cur !== main && cur !== document.body && !used.has(cur)) {
        used.add(cur);
        containers.push(cur);
      }
    }
    if (containers.length > 1) tops = containers;
  }
  // Sort by document-order Y so sections come out top→bottom.
  tops.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
  for (let i = 0; i < tops.length && out.sections.length < 20; i++) {
    const el = tops[i];
    const r = el.getBoundingClientRect();
    const y = Math.round(r.top + window.scrollY);
    let name = null;
    const ref = el.getAttribute('aria-labelledby');
    if (ref) {
      const t = document.getElementById(ref);
      if (t) name = kebab(t.textContent);
    }
    if (!name) {
      const h = el.querySelector('h1, h2');
      if (h) name = kebab(h.textContent);
    }
    if (!name) name = 'section-' + (out.sections.length + 1);
    const head = el.querySelector('h1, h2, h3');
    out.sections.push({
      name, y, height: Math.round(r.height),
      dom_path: domPath(el),
      heading_text: head ? (head.textContent || '').trim().slice(0, 220) : '',
    });
  }

  // --- hovers: elements that gate visibility of a sibling/descendant ---
  const all = document.querySelectorAll('*');
  let hovScanned = 0;
  for (const el of all) {
    if (out.hovers.length >= 20 || hovScanned >= 3000) break;
    hovScanned++;
    const s = getComputedStyle(el);
    if (s.cursor !== 'pointer') continue;
    // candidate hidden child: role=menu / aria-hidden / display:none /
    // opacity:0 inside the trigger or its immediate sibling
    let hidden = null;
    const candDesc = el.querySelectorAll('[role="menu"], [role="listbox"], [aria-hidden="true"], [aria-expanded]');
    for (const d of candDesc) {
      const ds = getComputedStyle(d);
      if (isHidden(ds) || d.getAttribute('aria-hidden') === 'true') { hidden = d; break; }
    }
    if (!hidden && el.parentElement) {
      for (const sib of el.parentElement.children) {
        if (sib === el) continue;
        const ss = getComputedStyle(sib);
        if (sib.getAttribute('role') === 'menu'
            || sib.getAttribute('aria-hidden') === 'true'
            || isHidden(ss)) { hidden = sib; break; }
      }
    }
    if (!hidden) continue;
    const triggerText = (el.textContent || '').trim().slice(0, 80);
    const label = kebab(triggerText) || ('hover-' + (out.hovers.length + 1));
    out.hovers.push({
      label,
      trigger_text: triggerText,
      trigger_selector: domPath(el),
      hidden_child_selector: domPath(hidden),
      revealed_text: '',
      revealed_items: [],
      position_hint: 'below',
    });
  }

  // --- animations: keyframes + meaningful transitions ---
  const seenAnim = new Set();
  let animScanned = 0;
  for (const el of all) {
    if (out.animations.length >= 40 || animScanned >= 5000) break;
    animScanned++;
    const s = getComputedStyle(el);
    const selPath = domPath(el);
    if (s.animationName && s.animationName !== 'none') {
      const k = selPath + '|' + s.animationName;
      if (seenAnim.has(k)) continue;
      seenAnim.add(k);
      let kf = '';
      for (const sheet of document.styleSheets) {
        let rules;
        try { rules = sheet.cssRules || []; } catch (_) { continue; }
        for (const r of rules) {
          if (r.type === 7 && r.name === s.animationName) { kf = r.cssText; break; }
        }
        if (kf) break;
      }
      let trig = 'load';
      if (el.hasAttribute('data-aos') || el.hasAttribute('data-animate-on-scroll')
          || (el.className || '').toString().match(/\b(aos|reveal|scroll-anim|in-view)\b/)) {
        trig = 'scroll';
      }
      out.animations.push({
        selector: selPath, kind: 'animation', name: s.animationName,
        duration: s.animationDuration, easing: s.animationTimingFunction,
        delay: s.animationDelay, iterations: s.animationIterationCount,
        keyframes_css: kf, trigger_guess: trig,
      });
    } else if (s.transitionProperty && s.transitionProperty !== 'none'
               && parseFloat(s.transitionDuration || '0') > 0) {
      const k = selPath + '|' + s.transitionProperty;
      if (seenAnim.has(k)) continue;
      seenAnim.add(k);
      out.animations.push({
        selector: selPath, kind: 'transition', name: s.transitionProperty,
        duration: s.transitionDuration, easing: s.transitionTimingFunction,
        delay: s.transitionDelay, iterations: '1',
        keyframes_css: '', trigger_guess: 'hover',
      });
    }
  }

  // --- videos ---
  for (const v of document.querySelectorAll('video')) {
    const src0 = v.currentSrc || v.src || (v.querySelector('source') && v.querySelector('source').src) || '';
    if (!src0) continue;
    let abs;
    try { abs = new URL(src0, location.href).href; } catch (_) { abs = src0; }
    out.videos.push({
      src: abs,
      poster: v.poster || '',
      width: v.videoWidth || Math.round(v.getBoundingClientRect().width) || 0,
      height: v.videoHeight || Math.round(v.getBoundingClientRect().height) || 0,
      autoplay: !!v.autoplay, loop: !!v.loop, muted: !!v.muted,
    });
  }

  // --- font_stack: computed fontFamily of top-level text elements ---
  for (const sel of ['h1', 'h2', 'h3', 'p', 'body']) {
    const el = document.querySelector(sel);
    if (!el) continue;
    const f = (getComputedStyle(el).fontFamily || '').replace(/\s+/g, ' ').trim();
    if (f) out.font_stack[sel] = f;
  }

  return out;
}
"""


# Asset harvest (images + inline SVGs + CSS background-images). Lifted from
# the prior tool with `<source>` and `<picture>` srcset additions.
_HARVEST_JS = r"""
() => {
  const out = {imgs: [], svgs: [], bgs: []};
  const seen = new Set();
  const abs = (u) => { try { return new URL(u, location.href).href; } catch (_) { return null; } };
  const roleFor = (el, r) => el.closest('header,nav') ? 'header'
                          : el.closest('footer') ? 'footer'
                          : (r.width > 600 ? 'hero' : 'content');

  // <img>
  for (const el of document.querySelectorAll('img')) {
    const src = abs(el.currentSrc || el.src);
    if (!src || seen.has(src)) continue;
    seen.add(src);
    const r = el.getBoundingClientRect();
    out.imgs.push({
      url: src, alt: (el.alt || '').slice(0, 240),
      width: Math.round(r.width), height: Math.round(r.height),
      role: roleFor(el, r),
    });
  }

  // <picture><source srcset="..."> — capture the largest variant per <source>
  for (const src of document.querySelectorAll('picture source[srcset]')) {
    const entries = (src.getAttribute('srcset') || '').split(',').map(s => s.trim()).filter(Boolean);
    let best = null, bestW = 0;
    for (const e of entries) {
      const parts = e.split(/\s+/);
      const u = abs(parts[0]);
      const w = parseInt((parts[1] || '0').replace(/\D/g, ''), 10) || 0;
      if (u && w >= bestW) { best = u; bestW = w; }
    }
    if (best && !seen.has(best)) {
      seen.add(best);
      const pic = src.closest('picture');
      const r = (pic || src).getBoundingClientRect();
      out.imgs.push({
        url: best, alt: '',
        width: Math.round(r.width), height: Math.round(r.height),
        role: roleFor(src, r),
      });
    }
  }

  // inline <svg>
  let svgIdx = 0;
  for (const el of document.querySelectorAll('svg')) {
    svgIdx += 1;
    const xml = new XMLSerializer().serializeToString(el);
    if (xml.length > 80000) continue;
    const r = el.getBoundingClientRect();
    out.svgs.push({
      index: svgIdx, xml,
      width: Math.round(r.width), height: Math.round(r.height),
      role: el.closest('header,nav') ? 'header'
            : el.closest('footer') ? 'footer' : 'content',
    });
  }

  // CSS background-image
  for (const el of document.querySelectorAll('*')) {
    const style = getComputedStyle(el);
    const bg = style.backgroundImage;
    if (!bg || bg === 'none') continue;
    const matches = bg.matchAll(/url\((['"]?)([^'")]+)\1\)/g);
    for (const m of matches) {
      const u = abs(m[2]);
      if (!u || seen.has(u)) continue;
      seen.add(u);
      const r = el.getBoundingClientRect();
      out.bgs.push({
        url: u,
        width: Math.round(r.width), height: Math.round(r.height),
        role: el === document.body ? 'body-bg' : 'bg',
      });
    }
  }

  return out;
}
"""


# Video harvest (returns sources distinct from inline <video> for poster
# binding). Runs once on the desktop visit.
_VIDEOS_JS = r"""
() => {
  const out = [];
  const abs = (u) => { try { return new URL(u, location.href).href; } catch (_) { return null; } };
  for (const v of document.querySelectorAll('video')) {
    let src = v.currentSrc || v.src;
    if (!src) {
      const s = v.querySelector('source');
      if (s) src = s.src || s.getAttribute('src');
    }
    src = abs(src);
    if (!src) continue;
    out.push({
      url: src,
      poster: abs(v.poster) || '',
      width: v.videoWidth || Math.round(v.getBoundingClientRect().width) || 0,
      height: v.videoHeight || Math.round(v.getBoundingClientRect().height) || 0,
      autoplay: !!v.autoplay, loop: !!v.loop, muted: !!v.muted,
    });
  }
  return out;
}
"""


# Font @font-face walk. Kept verbatim from the prior extract_site_fonts tool.
_FONTS_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const push = (fam, weight, style, url, fmt) => {
    const key = `${fam}|${weight}|${style}|${url}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({family: fam, weight, style, src_url: url, format: fmt});
  };
  for (const sheet of Array.from(document.styleSheets)) {
    let rules;
    try { rules = sheet.cssRules || []; } catch (_) { continue; }
    for (const r of Array.from(rules)) {
      if (r.type !== 5) continue;
      const fam = (r.style.fontFamily || '').replace(/['"]/g, '').trim();
      const weight = (r.style.fontWeight || '400').trim();
      const style = (r.style.fontStyle || 'normal').trim();
      const srcRaw = r.style.src || '';
      const m = srcRaw.matchAll(/url\((['"]?)([^'")]+)\1\)(?:\s*format\((['"]?)([^'")]+)\3\))?/g);
      for (const item of m) {
        let u; try { u = new URL(item[2], location.href).href; } catch (_) { continue; }
        const fmt = (item[4] || '').toLowerCase();
        push(fam, weight, style, u, fmt);
      }
    }
  }
  if (document.fonts && document.fonts.values) {
    for (const f of Array.from(document.fonts.values())) {
      const fam = (f.family || '').replace(/['"]/g, '').trim();
      const weight = (f.weight || '400').trim();
      const style = (f.style || 'normal').trim();
      const stub = `font-loaded:${fam}:${weight}:${style}`;
      if (![...seen].some(k => k.startsWith(`${fam}|${weight}|${style}|`))) {
        push(fam, weight, style, stub, '');
      }
    }
  }
  return out;
}
"""


# After hover, extract the revealed UI's text + links from the now-visible
# child element. Takes the parent-relative hidden selector; returns
# {revealed_text, revealed_items, position_hint, bbox}.
_HOVER_EXTRACT_JS = r"""
(args) => {
  const {trigger_selector, hidden_child_selector} = args;
  const trig = document.querySelector(trigger_selector);
  const menu = document.querySelector(hidden_child_selector);
  if (!menu) return null;
  const r = menu.getBoundingClientRect();
  const rt = trig ? trig.getBoundingClientRect() : null;
  let pos = 'below';
  if (rt) {
    if (r.left >= rt.right - 4) pos = 'right';
    else if (r.top >= rt.bottom - 4) pos = 'below';
    else pos = 'overlay';
  }
  const items = [];
  for (const a of menu.querySelectorAll('a, button')) {
    const t = (a.textContent || '').trim();
    if (!t) continue;
    items.push({text: t.slice(0, 120), href: a.getAttribute('href') || ''});
    if (items.length >= 24) break;
  }
  return {
    revealed_text: (menu.textContent || '').trim().slice(0, 400),
    revealed_items: items,
    position_hint: pos,
    bbox: {x: Math.round(r.left + window.scrollX),
           y: Math.round(r.top + window.scrollY),
           width: Math.round(r.width), height: Math.round(r.height)},
  };
}
"""


async def _upload_static(client: httpx.AsyncClient, gateway_url: str, headers: dict[str, str],
                          ac: str, cc: str, payload: bytes, filename: str, mime: str,
                          subdir: str = "clone") -> tuple[bool, str | None, str | None]:
    """POST raw bytes to /<app>/<client>/page/api/files/static/<app>/global/<subdir>/<filename>.
    Returns (ok, public_url, error_string). Used for images/videos/fonts —
    fonts pass subdir='fonts', images/videos default to 'clone'."""
    path_segment = f"/{ac}/global/{subdir}"
    upload_path = f"/{ac}/{cc}/page/api/files/static{path_segment}"
    req_headers = dict(headers)
    req_headers["clientCode"] = cc
    req_headers.pop("Content-Type", None)
    url = gateway_url + upload_path
    try:
        files = {"file": (filename, payload, mime or _MIME_OCTET)}
        resp = await client.post(url, headers=req_headers, files=files,
                                 params={"clientCode": cc, "override": "true"}, timeout=60.0)
    except Exception as e:  # noqa: BLE001
        return False, None, f"{type(e).__name__}: {e}"
    if resp.status_code >= 400:
        return False, None, f"HTTP {resp.status_code}: {resp.text[:200]}"
    dl_path = f"/api/files/static/file/{cc}{path_segment}/{filename}"
    return True, gateway_url + dl_path, None


async def _capture_full_page(page, wait_ms: int = 200) -> bytes:
    """Snap a full-page PNG. Playwright handles the scroll-and-stitch."""
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(wait_ms)
    return await page.screenshot(full_page=True, type="png")


async def _capture_viewport_at(page, y: int, wait_ms: int = 250) -> bytes:
    """Scroll to y and snap a viewport-aligned PNG."""
    await page.evaluate(f"window.scrollTo({{top: {y}, behavior: 'instant'}})")
    await page.wait_for_timeout(wait_ms)
    return await page.screenshot(full_page=False, type="png")


async def _capture_bbox(page, bbox: dict[str, int], padding: int = 12) -> bytes:
    """Clip to a bounding box plus padding. Used for hover-revealed UI shots.

    Note: Playwright's screenshot(clip=...) is relative to the viewport, so
    we scroll the bbox into view first.
    """
    x = max(0, bbox.get("x", 0) - padding)
    y = max(0, bbox.get("y", 0) - padding)
    w = bbox.get("width", 0) + padding * 2
    h = bbox.get("height", 0) + padding * 2
    if w <= 0 or h <= 0:
        return await page.screenshot(full_page=False, type="png")
    # Scroll the bbox into view (top of viewport)
    await page.evaluate(f"window.scrollTo({{top: {max(0, y - 40)}, behavior: 'instant'}})")
    await page.wait_for_timeout(150)
    return await page.screenshot(
        full_page=False, type="png",
        clip={"x": x, "y": padding, "width": w, "height": h},
    )


def _viewport_widths(params: dict[str, Any]) -> list[int]:
    raw = params.get("viewport_widths")
    if not raw:
        return [1440, 768, 375]
    if not isinstance(raw, list):
        return [1440, 768, 375]
    out: list[int] = []
    for v in raw:
        try:
            w = int(v)
        except Exception:  # noqa: BLE001
            continue
        if 320 <= w <= 3840:
            out.append(w)
    return out or [1440, 768, 375]


async def _trigger_lazy_loads(page) -> None:
    """Scroll top-to-bottom to trigger lazy-loaded imagery + animations."""
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


def _derive_font_meta_from_url(url: str) -> tuple[str, str, str]:
    """Network-listener fonts have no family/weight/style metadata. Guess
    them from the filename (Inter-Regular.woff2 → Inter / 400 / normal)."""
    name_re = re.compile(
        r"([A-Za-z][A-Za-z0-9_-]+?)[-_]"
        r"(Thin|ExtraLight|Light|Regular|Medium|SemiBold|Bold|ExtraBold|Black|Variable|\d{3})"
        r"(Italic)?", re.IGNORECASE,
    )
    weight_map = {"thin": "100", "extralight": "200", "light": "300", "regular": "400",
                  "medium": "500", "semibold": "600", "bold": "700", "extrabold": "800",
                  "black": "900"}
    fname = url.rsplit("/", 1)[-1].split("?")[0]
    m = name_re.search(fname)
    if not m:
        family = re.sub(r"\.(woff2?|ttf|otf|eot).*$", "", fname, flags=re.I).replace("-", " ")
        return family, "400", "normal"
    fam_raw = m.group(1)
    weight_raw = m.group(2)
    italic = bool(m.group(3))
    family = re.sub(r"([a-z])([A-Z])", r"\1 \2", fam_raw)
    weight = weight_map.get(weight_raw.lower(), weight_raw if weight_raw.isdigit() else "400")
    style = "italic" if italic else "normal"
    return family, weight, style


async def _execute_extract_site_assets(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:  # noqa: PLR0912, PLR0915, C901
    url = (params.get("url") or "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return ToolResult(success=False, error="`url` must be absolute http(s)")
    viewport_widths = _viewport_widths(params)
    viewport_height = max(320, min(int(params.get("viewport_height") or 900), 2160))
    wait_ms = max(500, min(int(params.get("wait_ms") or 2500), 30000))
    max_assets = max(1, min(int(params.get("max_assets") or 80), 200))

    ac = (params.get("app_code") or context.get("app_code") or "").strip()
    cc = (params.get("client_code") or context.get("client_code") or "").strip()
    if not ac:
        return ToolResult(success=False, error="No app_code in context")
    headers = dict(context.get("headers") or {})

    from app.config import settings
    gateway_url = (getattr(settings, "GATEWAY_URL", "") or "").rstrip("/")
    if not gateway_url:
        return ToolResult(success=False, error="GATEWAY_URL not configured")

    progress = context.get("progress") or (lambda _m: None)

    # session_id for shot-cache lookups
    session_id = ""
    sc = context.get("session_context") if isinstance(context, dict) else None
    if isinstance(sc, dict):
        session_id = str(sc.get("session_id") or sc.get("_session_id") or "")
    if not session_id:
        session_id = "_unattached_"

    url_slug = _handle_slug(url)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return ToolResult(success=False, error="playwright not installed")

    viewport_manifests: dict[str, dict[str, Any]] = {}
    desktop_harvest: dict[str, list[dict[str, Any]]] = {"imgs": [], "svgs": [], "bgs": []}
    raw_fonts_js: list[dict[str, Any]] = []
    network_fonts: list[dict[str, Any]] = []
    desktop_videos_js: list[dict[str, Any]] = []
    desktop_width = viewport_widths[0]

    progress(f"Launching headless browser → {url}")
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            ctx_b = await browser.new_context(
                viewport={"width": desktop_width, "height": viewport_height},
                ignore_https_errors=True, user_agent=_USER_AGENT,
            )
            page = await ctx_b.new_page()

            # Font network listener — catches Cloudflare-served fonts the
            # CSS walker misses. Attached for the whole session.
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

            for w_idx, w in enumerate(viewport_widths):
                w_str = str(w)
                progress(f"Viewport {w}px — visiting {url}")
                # Playwright exposes set_viewport_size on Page (not Context).
                await page.set_viewport_size({"width": w, "height": viewport_height})
                try:
                    await page.goto(url, wait_until="networkidle", timeout=30000)
                except Exception:  # noqa: BLE001
                    pass  # ad-heavy sites never settle
                await page.wait_for_timeout(wait_ms)
                await _trigger_lazy_loads(page)

                # Recon (sections, hovers, animations, videos, font_stack)
                try:
                    recon = await page.evaluate(_RECON_JS)
                except Exception as e:  # noqa: BLE001
                    progress(f"  recon JS failed: {type(e).__name__}: {e}")
                    recon = {"sections": [], "hovers": [], "animations": [],
                             "videos": [], "font_stack": {}}
                progress(f"  recon: sections={len(recon.get('sections', []))} "
                         f"hovers={len(recon.get('hovers', []))} "
                         f"animations={len(recon.get('animations', []))}")

                doc_h_val = await page.evaluate("document.documentElement.scrollHeight")
                try:
                    doc_h = int(doc_h_val)
                except Exception:  # noqa: BLE001
                    doc_h = 0

                # Full-page screenshot
                progress(f"  full-page screenshot at {w}px")
                fp_png = await _capture_full_page(page)
                fp_handle = f"{url_slug}:fullpage_w{w}"
                put_shot(session_id, fp_handle, {
                    "url": url, "viewport_width": w, "kind": "fullpage",
                    "image_base64": base64.b64encode(fp_png).decode("ascii"),
                    "image_mime": _MIME_PNG,
                })

                # Per-section screenshots
                sections_out: list[dict[str, Any]] = []
                for s in recon.get("sections", []):
                    s_y = int(s.get("y") or 0)
                    s_name = s.get("name") or f"section-{len(sections_out)+1}"
                    s_handle = f"{url_slug}:section_{s_name}_w{w}"
                    try:
                        s_png = await _capture_viewport_at(page, s_y)
                    except Exception as e:  # noqa: BLE001
                        progress(f"    section {s_name}: screenshot failed: {type(e).__name__}")
                        continue
                    put_shot(session_id, s_handle, {
                        "url": url, "viewport_width": w, "kind": "section",
                        "section_name": s_name, "y": s_y,
                        "image_base64": base64.b64encode(s_png).decode("ascii"),
                        "image_mime": _MIME_PNG,
                    })
                    sections_out.append({
                        "name": s_name, "y": s_y, "height": s.get("height"),
                        "dom_path": s.get("dom_path"),
                        "heading_text": s.get("heading_text"),
                        "handle": s_handle,
                    })
                progress(f"  captured {len(sections_out)} section screenshots")

                # Hover screenshots — drive hover, screenshot the revealed area,
                # extract revealed_text / revealed_items / position_hint.
                hovers_out: list[dict[str, Any]] = []
                for h in recon.get("hovers", []):
                    label = h.get("label") or f"hover-{len(hovers_out)+1}"
                    trig_sel = h.get("trigger_selector") or ""
                    hidden_sel = h.get("hidden_child_selector") or ""
                    if not trig_sel or not hidden_sel:
                        continue
                    try:
                        locator = page.locator(trig_sel).first
                        await locator.scroll_into_view_if_needed(timeout=2000)
                        await locator.hover(timeout=2000)
                        await page.wait_for_timeout(400)
                    except Exception as e:  # noqa: BLE001
                        progress(f"    hover {label}: trigger failed: {type(e).__name__}")
                        continue
                    try:
                        details = await page.evaluate(
                            _HOVER_EXTRACT_JS,
                            {"trigger_selector": trig_sel, "hidden_child_selector": hidden_sel},
                        )
                    except Exception:  # noqa: BLE001
                        details = None
                    bbox = (details or {}).get("bbox") if details else None
                    try:
                        if bbox and bbox.get("width", 0) > 0 and bbox.get("height", 0) > 0:
                            shot_png = await _capture_bbox(page, bbox)
                        else:
                            shot_png = await page.screenshot(full_page=False, type="png")
                    except Exception as e:  # noqa: BLE001
                        progress(f"    hover {label}: screenshot failed: {type(e).__name__}")
                        # release the hover by moving the mouse away
                        try:
                            await page.mouse.move(0, 0)
                        except Exception:  # noqa: BLE001
                            pass
                        continue
                    h_handle = f"{url_slug}:hover_{label}_w{w}"
                    put_shot(session_id, h_handle, {
                        "url": url, "viewport_width": w, "kind": "hover",
                        "hover_label": label,
                        "image_base64": base64.b64encode(shot_png).decode("ascii"),
                        "image_mime": _MIME_PNG,
                    })
                    hovers_out.append({
                        "label": label,
                        "trigger_text": h.get("trigger_text"),
                        "trigger_selector": trig_sel,
                        "hidden_child_selector": hidden_sel,
                        "handle": h_handle,
                        "revealed_text": (details or {}).get("revealed_text", "") if details else "",
                        "revealed_items": (details or {}).get("revealed_items", []) if details else [],
                        "position_hint": (details or {}).get("position_hint", "below") if details else "below",
                    })
                    # release hover for next iteration
                    try:
                        await page.mouse.move(0, 0)
                        await page.wait_for_timeout(150)
                    except Exception:  # noqa: BLE001
                        pass
                progress(f"  captured {len(hovers_out)} hover screenshots")

                viewport_manifests[w_str] = {
                    "fullpage_handle": fp_handle,
                    "doc_height": doc_h,
                    "sections": sections_out,
                    "hovers": hovers_out,
                    "animations": recon.get("animations", []),
                    "font_stack": recon.get("font_stack", {}),
                }

                # On desktop visit only: harvest assets + fonts.
                if w_idx == 0:
                    progress("  harvesting assets (imgs/svgs/bgs)")
                    try:
                        desktop_harvest = await page.evaluate(_HARVEST_JS)
                    except Exception as e:  # noqa: BLE001
                        progress(f"  harvest JS failed: {type(e).__name__}")
                    progress("  harvesting videos")
                    try:
                        desktop_videos_js = await page.evaluate(_VIDEOS_JS) or []
                    except Exception as e:  # noqa: BLE001
                        progress(f"  videos JS failed: {type(e).__name__}")
                    progress("  harvesting @font-face rules")
                    try:
                        raw_fonts_js = await page.evaluate(_FONTS_JS) or []
                    except Exception as e:  # noqa: BLE001
                        progress(f"  fonts JS failed: {type(e).__name__}")

            await browser.close()
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"render error: {type(e).__name__}: {e}")

    # ---- Asset upload pass ----
    items: list[dict[str, Any]] = []
    items += [{"kind": "img", **e} for e in (desktop_harvest.get("imgs") or [])]
    items += [{"kind": "bg",  **e} for e in (desktop_harvest.get("bgs")  or [])]
    items += [{"kind": "svg", **e} for e in (desktop_harvest.get("svgs") or [])]
    items = items[:max_assets]

    progress(f"Uploading {len(items)} assets + {len(desktop_videos_js)} videos to Modlix files")

    seen_sha: dict[str, str] = {}
    originals: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(8)

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True,
                                  headers={"User-Agent": _USER_AGENT}) as fetch_client, \
               httpx.AsyncClient(timeout=60.0) as upload_client:

        async def _process_asset(item: dict[str, Any]) -> dict[str, Any] | None:
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
                        mime = (r.headers.get("content-type") or "").split(";")[0].strip().lower() or _MIME_OCTET
                        if not mime.startswith("image/"):
                            return {"src": item["url"], "error": f"non-image: {mime}"}
                        base_name = "img"
                        origin = item["url"]
                    sha = _sha8(payload)
                    if sha in seen_sha:
                        return {"src": origin, "kind": item["kind"], "modlix_url": seen_sha[sha],
                                "skipped": "dedup", "sha8": sha,
                                "width": item.get("width"), "height": item.get("height"),
                                "role": item.get("role"), "alt": item.get("alt")}
                    filename = f"{base_name}_{sha}.{_ext_for_mime(mime)}"
                    ok, modlix_url, err = await _upload_static(
                        upload_client, gateway_url, headers, ac, cc, payload, filename, mime,
                    )
                    if not ok:
                        return {"src": origin, "error": err or _ERR_UPLOAD_FAILED}
                    seen_sha[sha] = modlix_url
                    return {"src": origin, "kind": item["kind"], "modlix_url": modlix_url,
                            "mime": mime, "bytes": len(payload), "sha8": sha,
                            "width": item.get("width"), "height": item.get("height"),
                            "role": item.get("role"), "alt": item.get("alt")}
                except Exception as e:  # noqa: BLE001
                    return {"src": item.get("url") or "?", "error": f"{type(e).__name__}: {e}"}

        # ---- Video upload pass ----
        seen_vsha: dict[str, str] = {}
        videos_out: list[dict[str, Any]] = []
        video_failures: list[dict[str, Any]] = []

        async def _process_video(v: dict[str, Any]) -> dict[str, Any] | None:
            async with sem:
                src = v.get("url")
                if not src:
                    return None
                try:
                    r = await fetch_client.get(src)
                    if r.status_code >= 400:
                        return {"src": src, "error": f"HTTP {r.status_code}"}
                    payload = r.content
                    mime = (r.headers.get("content-type") or "").split(";")[0].strip().lower() or "video/mp4"
                    sha = _sha8(payload)
                    if sha in seen_vsha:
                        return {"src": src, "modlix_url": seen_vsha[sha], "skipped": "dedup"}
                    filename = f"vid_{sha}.{_ext_for_mime(mime)}"
                    ok, modlix_url, err = await _upload_static(
                        upload_client, gateway_url, headers, ac, cc, payload, filename, mime,
                    )
                    if not ok:
                        return {"src": src, "error": err or _ERR_UPLOAD_FAILED}
                    seen_vsha[sha] = modlix_url
                    # poster: fetch + upload as a separate image if present
                    poster_url = None
                    if v.get("poster"):
                        try:
                            pr = await fetch_client.get(v["poster"])
                            if pr.status_code < 400:
                                p_payload = pr.content
                                p_mime = (pr.headers.get("content-type") or "image/jpeg").split(";")[0].strip().lower()
                                p_sha = _sha8(p_payload)
                                if p_sha not in seen_sha:
                                    p_fname = f"img_{p_sha}.{_ext_for_mime(p_mime)}"
                                    pok, p_url, _ = await _upload_static(
                                        upload_client, gateway_url, headers, ac, cc,
                                        p_payload, p_fname, p_mime,
                                    )
                                    if pok:
                                        seen_sha[p_sha] = p_url
                                        poster_url = p_url
                                else:
                                    poster_url = seen_sha[p_sha]
                        except Exception:  # noqa: BLE001
                            pass
                    return {"src": src, "kind": "video", "modlix_url": modlix_url,
                            "poster_url": poster_url, "mime": mime, "bytes": len(payload),
                            "sha8": sha, "width": v.get("width"), "height": v.get("height"),
                            "autoplay": v.get("autoplay"), "loop": v.get("loop"),
                            "muted": v.get("muted")}
                except Exception as e:  # noqa: BLE001
                    return {"src": src, "error": f"{type(e).__name__}: {e}"}

        # ---- Font merge + upload pass ----
        merged_fonts: list[dict[str, Any]] = list(raw_fonts_js)
        seen_urls = {f.get("src_url") for f in merged_fonts}
        for nf in network_fonts:
            u = nf["url"]
            if u in seen_urls:
                continue
            seen_urls.add(u)
            family, weight, style = _derive_font_meta_from_url(u)
            merged_fonts.append({
                "family": family, "weight": weight, "style": style,
                "src_url": u, "format": _ext_for_font_url(u),
            })
        downloadable_fonts = [
            f for f in merged_fonts
            if isinstance(f.get("src_url"), str)
            and f["src_url"].startswith(("http://", "https://"))
        ][:60]

        fonts_out: list[dict[str, Any]] = []
        font_failures: list[dict[str, Any]] = []
        seen_fsha: dict[str, str] = {}

        async def _process_font(f: dict[str, Any]) -> dict[str, Any] | None:
            async with sem:
                try:
                    r = await fetch_client.get(f["src_url"])
                    if r.status_code >= 400:
                        return {**f, "error": f"HTTP {r.status_code}"}
                    payload = r.content
                    sha = _sha8(payload)
                    if sha in seen_fsha:
                        return {**f, "modlix_url": seen_fsha[sha], "skipped": "dedup"}
                    ext = _EXT_FOR_FONT_FORMAT.get(f.get("format", ""), _ext_for_font_url(f["src_url"]))
                    fam_slug = re.sub(r"[^a-zA-Z0-9]+", "_", f["family"]).strip("_") or "font"
                    weight = f.get("weight") or "400"
                    style = f.get("style") or "normal"
                    filename = f"{fam_slug}_{weight}_{style}_{sha}.{ext}"
                    ok, modlix_url, err = await _upload_static(
                        upload_client, gateway_url, headers, ac, cc,
                        payload, filename, _MIME_OCTET,
                        subdir="fonts",
                    )
                    if not ok:
                        return {**f, "error": err or _ERR_UPLOAD_FAILED}
                    seen_fsha[sha] = modlix_url
                    return {**f, "modlix_url": modlix_url, "bytes": len(payload), "sha8": sha}
                except Exception as e:  # noqa: BLE001
                    return {**f, "error": f"{type(e).__name__}: {e}"}

        # Run all three pipelines in parallel for max throughput.
        asset_results, video_results, font_results = await asyncio.gather(
            asyncio.gather(*(_process_asset(i) for i in items)) if items else asyncio.gather(),
            asyncio.gather(*(_process_video(v) for v in desktop_videos_js)) if desktop_videos_js else asyncio.gather(),
            asyncio.gather(*(_process_font(f) for f in downloadable_fonts)) if downloadable_fonts else asyncio.gather(),
        )

    for r in (asset_results or []):
        if not r:
            continue
        (failures if "error" in r else originals).append(r)
    for r in (video_results or []):
        if not r:
            continue
        (video_failures if "error" in r else videos_out).append(r)
    for r in (font_results or []):
        if not r:
            continue
        (font_failures if "error" in r else fonts_out).append(r)

    progress(f"Uploaded {len(originals)} assets, {len(videos_out)} videos, {len(fonts_out)} fonts")

    # ---- Build fontPacks_suggested ----
    by_family: dict[str, list[dict[str, Any]]] = {}
    for f in fonts_out:
        if f.get("skipped"):
            continue
        by_family.setdefault(f["family"], []).append(f)
    fontPacks_suggested: dict[str, dict[str, str]] = {}
    for family, faces in by_family.items():
        rules: list[str] = []
        for face in faces:
            fmt = face.get("format") or _ext_for_font_url(face["modlix_url"])
            rules.append(
                f"@font-face{{font-family:'{family}';"
                f"src:url('{face['modlix_url']}') format('{fmt}');"
                f"font-weight:{face.get('weight', '400')};"
                f"font-style:{face.get('style', 'normal')};"
                f"font-display:swap;}}"
            )
        fontPacks_suggested[str(_uuid_lib.uuid4())] = {
            "name": family,
            "code": f"<style>{''.join(rules)}</style>",
        }

    # ---- Build summary (LLM-readable) ----
    lines: list[str] = [
        f"Recon of {url} across viewports {viewport_widths}.",
        f"Assets: {len(originals)} ok / {len(failures)} failed.",
        f"Videos: {len(videos_out)} ok / {len(video_failures)} failed.",
        f"Fonts: {len(fonts_out)} ok / {len(font_failures)} failed → "
        f"{len(fontPacks_suggested)} font famil{'ies' if len(fontPacks_suggested) != 1 else 'y'}.",
        "",
        "Per-viewport handles:",
    ]
    for w_str, vm in viewport_manifests.items():
        lines.append(
            f"  w={w_str}: fullpage={vm['fullpage_handle']}  "
            f"sections={len(vm['sections'])} hovers={len(vm['hovers'])} "
            f"animations={len(vm['animations'])}"
        )
        for s in vm["sections"][:6]:
            lines.append(f"    section {s['name']}: handle={s['handle']}  "
                         f"y={s['y']} h={s.get('height')}")
        if len(vm["sections"]) > 6:
            lines.append(f"    ... +{len(vm['sections']) - 6} more sections")
        for h in vm["hovers"][:4]:
            lines.append(f"    hover {h['label']}: handle={h['handle']}  "
                         f"items={len(h.get('revealed_items') or [])}")
    lines.append("")
    lines.append("Top assets (largest first; bind these into Image components — never invent URLs):")
    top_assets = sorted([o for o in originals if not o.get("skipped")],
                        key=lambda o: (o.get("width") or 0) * (o.get("height") or 0),
                        reverse=True)
    for o in top_assets[:14]:
        lines.append(
            f"  {o.get('role', '?'):>8}  {o.get('width') or '?'}x{o.get('height') or '?'}  "
            f"{o['modlix_url']}"
        )
    if len(top_assets) > 14:
        lines.append(f"  ... +{len(top_assets) - 14} more (see result.data.assets)")
    if videos_out:
        lines.append("")
        lines.append("Videos:")
        for v in videos_out[:6]:
            lines.append(f"  {v.get('width', '?')}x{v.get('height', '?')}  "
                         f"{v['modlix_url']}  poster={v.get('poster_url') or '-'}")
    if fontPacks_suggested:
        lines.append("")
        lines.append("fontPacks_suggested (paste verbatim into app.properties.fontPacks):")
        lines.append(f"  {list(fontPacks_suggested.keys())}")
    if failures or video_failures or font_failures:
        lines.append("")
        lines.append("Sample failures:")
        for f in (failures[:3] + video_failures[:2] + font_failures[:2]):
            lines.append(f"  {f.get('src', f.get('src_url', '?'))[:60]}  → "
                         f"{(f.get('error') or '?')[:80]}")

    progress("Recon complete.")

    return ToolResult(
        success=True,
        summary="\n".join(lines),
        data={
            "url": url,
            "viewports": viewport_manifests,
            "assets": originals,
            "videos": videos_out,
            "fontPacks_suggested": fontPacks_suggested,
            "asset_failures": failures,
            "video_failures": video_failures,
            "font_failures": font_failures,
        },
    )


extract_site_assets_tool = ToolDefinition(
    name="extract_site_assets",
    description=(
        "Unified Playwright recon for an external site. ONE call:\n"
        "- visits the URL at multiple viewport widths (desktop+tablet+mobile by default)\n"
        "- returns a structural manifest per viewport: `sections[]` (with per-section "
        "screenshot handles), `hovers[]` (with hover-state screenshots + the menu items "
        "revealed on hover), `animations[]` (keyframes + transitions + scroll-triggers, "
        "with `keyframes_css` ready to paste into a global style doc), `font_stack`.\n"
        "- captures a FULL-PAGE screenshot per viewport.\n"
        "- harvests every `<img>`, inline `<svg>`, CSS background-image, `<video>`, and "
        "`<picture><source srcset>` entry; uploads each to "
        "`<app>/global/clone/<sha>.<ext>` (videos go through the same path).\n"
        "- harvests web fonts via dual-path (CSS @font-face + network listener); uploads "
        "under `<app>/global/fonts/`; returns a `fontPacks_suggested` dict ready to PUT "
        "into `app.properties.fontPacks`.\n\n"
        "Use ONCE at the start of a clone. Bind the returned `modlix_url` values into "
        "Image / Video components — never invent URLs. Build hover-revealed UI from "
        "`hovers[].revealed_items`; wire animations using `animations[].keyframes_css` "
        "and the `clone-render-hovers-and-animations` workflow in platform KB."
    ),
    parameters=[
        ToolParameter(name="url", type="string", description="Absolute http(s) URL"),
        ToolParameter(name="viewport_widths", type="array", required=False,
                      default=[1440, 768, 375],
                      description="Viewport widths to recon (CSS px each in 320-3840).",
                      items={"type": "integer"}),
        ToolParameter(name="viewport_height", type="integer", required=False, default=900,
                      description="Render viewport height (CSS px, 320-2160)."),
        ToolParameter(name="max_assets", type="integer", required=False, default=80,
                      description="Cap on harvested static assets (1-200)."),
        ToolParameter(name="wait_ms", type="integer", required=False, default=2500,
                      description="Wait after load before harvesting (ms)."),
        ToolParameter(name="app_code", type="string", required=False,
                      description="Override target app code (defaults to session)."),
        ToolParameter(name="client_code", type="string", required=False,
                      description="Override target client code (defaults to session)."),
    ],
    execute=_execute_extract_site_assets,
)
