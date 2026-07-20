"""Full-DOM capture for faithful visual reconstruction.

Unlike the pruned component plan (segment.py) used for Modlix mapping, this
captures EVERY visible element (nested, including body and all wrapper/layout
containers) with its authored CSS, so a render reproduces the page faithfully
(centering, backgrounds, font inheritance, layout containers are all preserved).

One DOM.getDocument maps data-mxa-id -> nodeId; one getMatchedStylesForNode per
node resolves authored CSS at desktop/tablet/mobile. Hover is off by default
(irrelevant to a static screenshot).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from app.services.page_analyzer.models import ComponentNode

logger = logging.getLogger(__name__)

# Stamp every element (incl <html>), build a nested tree of VISIBLE elements.
# <svg> is captured whole (outerHTML) without recursing into its children.
_FULL_DOM_JS = r"""
() => {
  let i = 0;
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','HEAD','META','LINK','TITLE','BASE']);
  const stamp = (el) => {
    let id = el.getAttribute('data-mxa-id');
    if (!id) { id = 'f' + (i++); el.setAttribute('data-mxa-id', id); }
    return id;
  };
  const node = (el) => {
    const tag = el.tagName.toLowerCase();
    const o = { id: stamp(el), tag };
    let t = '';
    for (const n of el.childNodes) if (n.nodeType === 3) t += n.nodeValue;
    t = t.replace(/\s+/g, ' ').trim();
    if (t) o.text = t.slice(0, 2000);
    const href = el.getAttribute && el.getAttribute('href'); if (href) o.href = href;
    const src = el.currentSrc || el.getAttribute('src'); if (src) o.src = src;
    const alt = el.getAttribute && el.getAttribute('alt'); if (alt) o.alt = alt;
    if (tag === 'input') o.type = el.getAttribute('type') || 'text';
    if (tag === 'svg') {
      // Inline computed paint onto svg + descendants so the markup is
      // self-contained (external .class fill/stroke rules aren't captured).
      const PAINT = ['fill','stroke','stroke-width','stroke-dasharray','stroke-linecap',
        'stroke-linejoin','opacity','fill-opacity','stroke-opacity','transform',
        'transform-origin','color','mix-blend-mode'];
      const inlinePaint = (n) => {
        if (n.nodeType !== 1) return;
        const cs = getComputedStyle(n);
        let s = n.getAttribute('style') || '';
        for (const p of PAINT) { const v = cs.getPropertyValue(p); if (v) s += p + ':' + v + ';'; }
        n.setAttribute('style', s);
        for (const c of n.children) inlinePaint(c);
      };
      // Give the svg explicit intrinsic size + viewBox so it renders at the
      // right dimensions as a standalone data: URI (outside the page's CSS).
      const sr = el.getBoundingClientRect();
      if (sr.width && sr.height) {
        if (!el.getAttribute('viewBox')) el.setAttribute('viewBox', `0 0 ${Math.round(sr.width)} ${Math.round(sr.height)}`);
        el.setAttribute('width', Math.round(sr.width));
        el.setAttribute('height', Math.round(sr.height));
        el.setAttribute('preserveAspectRatio', 'xMidYMid meet');
      }
      inlinePaint(el);
      o.svg = el.outerHTML.slice(0, 120000);
      return o;
    }
    const kids = [];
    for (const c of el.children) {
      if (c.nodeType !== 1 || SKIP.has(c.tagName)) continue;
      if (i >= 6000) break;  // node cap
      // Include display:none elements too: responsive-swapped content (e.g. a
      // mobile hamburger that's hidden at desktop) is shown at the breakpoint
      // where its resolved `display` is not none. JS-toggled modals stay hidden
      // at every sampled width, so they render hidden — correct.
      kids.push(node(c));
    }
    if (kids.length) o.children = kids;
    return o;
  };
  return node(document.documentElement);
}
"""

_ROOT_VARS_JS = r"""
() => {
  const cs = getComputedStyle(document.documentElement);
  const out = {};
  for (const p of cs) {
    if (p.startsWith('--')) out[p] = cs.getPropertyValue(p).trim();
  }
  return out;
}
"""

_FONTFACE_JS = r"""
() => {
  const faces = [];
  for (const sheet of document.styleSheets) {
    let rules = null;
    try { rules = sheet.cssRules; } catch (e) { continue; }
    if (!rules) continue;
    for (const r of rules) {
      if (r.type === 5 /* CSSFontFaceRule */ && r.cssText) faces.push(r.cssText);
    }
  }
  return faces.slice(0, 80);
}
"""

# Harvest @keyframes (CSS animations). Walks top-level + @media/@supports nested.
_KEYFRAMES_JS = r"""
() => {
  const out = [];
  const walk = (rules) => {
    for (const r of rules || []) {
      if (r.type === 7 /* CSSKeyframesRule */ && r.cssText) out.push(r.cssText);
      else if ((r.type === 4 || r.type === 12) && r.cssRules) walk(r.cssRules); // @media/@supports
    }
  };
  for (const sheet of document.styleSheets) {
    try { walk(sheet.cssRules); } catch (e) { continue; }
  }
  return out.slice(0, 300);
}
"""

# Tags whose :hover styling is worth capturing (keeps the hover pass cheap).
INTERACTIVE_TAGS = {"a", "button", "summary", "label", "select", "input"}


_FONT_MIME = {"woff2": "font/woff2", "woff": "font/woff", "ttf": "font/ttf", "otf": "font/otf"}


async def embed_font_faces(faces: List[str], base_url: str) -> List[str]:
    """Fetch each @font-face's font file (server-side, no CORS) and inline it as
    a base64 data: URI, so the font loads from a file:// preview. Falls back to
    the original (absolutized) rule on any failure."""
    if not faces:
        return []
    import base64

    import httpx

    out: List[str] = []
    async with httpx.AsyncClient(timeout=20.0, verify=False, follow_redirects=True) as client:
        for face in faces:
            m = re.search(r"""url\(["']?([^"')]+)["']?\)""", face)
            if not m:
                out.append(face)
                continue
            url = m.group(1).strip()
            if not url.startswith(("http://", "https://", "data:")):
                url = urljoin(base_url, url)
            if url.startswith("data:"):
                out.append(face)
                continue
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                ext = url.rsplit(".", 1)[-1].split("?")[0].lower()
                mime = _FONT_MIME.get(ext, "application/octet-stream")
                b64 = base64.b64encode(resp.content).decode("ascii")
                data = f'url("data:{mime};base64,{b64}")'
                out.append(re.sub(r"""url\(["']?[^"')]+["']?\)""", data, face, count=1))
                logger.info("embedded font %s (%d KB)", url.rsplit("/", 1)[-1], len(resp.content) // 1024)
            except Exception as exc:  # noqa: BLE001
                logger.debug("font embed failed for %s: %s", url, exc)
                out.append(absolutize_css_urls(face, base_url))
    return out


def absolutize_css_urls(value: str, base_url: str) -> str:
    if not base_url or "url(" not in value:
        return value

    def repl(m: "re.Match") -> str:
        raw = m.group(1).strip().strip("'\"")
        if raw.startswith(("http://", "https://", "data:")):
            return m.group(0)
        return f'url("{urljoin(base_url, raw)}")'

    return re.sub(r"url\(([^)]*)\)", repl, value)


def _flatten_spec(spec: Dict[str, Any], out: List[Dict[str, Any]]) -> None:
    out.append(spec)
    for child in spec.get("children") or []:
        _flatten_spec(child, out)


async def _resolve_all_breakpoints(extractor, page, bps, all_ids, interactive_ids, cap):
    """For each viewport: resize, then resolve every node's authored CSS at that
    width (CDP only returns viewport-active media rules, so we MUST resize).
    Returns (per_bp: {name: {id: decls}}, hover_map: {id: delta})."""
    per_bp: Dict[str, Dict[str, Dict[str, str]]] = {}
    hover_map: Dict[str, Dict[str, str]] = {}
    for i, bp in enumerate(bps):
        name, w, h = str(bp["name"]), int(bp["width"]), int(bp["height"])
        await page.set_viewport_size({"width": w, "height": h})
        await page.wait_for_timeout(500)
        idmap = await extractor.id_map()  # rebuild: DOM may re-render on resize
        resolved: Dict[str, Dict[str, str]] = {}
        n = 0
        for mid in all_ids:
            if n >= cap:
                break
            nid = idmap.get(mid)
            if not nid:
                continue
            resolved[mid] = await extractor.resolved_at(nid, w)
            n += 1
        per_bp[name] = resolved
        if i == 0:  # capture hover on the base (desktop) viewport only
            for mid in interactive_ids:
                nid = idmap.get(mid)
                if not nid:
                    continue
                hv = await extractor.hover_at(nid, w)
                base = resolved.get(mid, {})
                delta = {k: v for k, v in hv.items() if base.get(k) != v}
                if delta:
                    hover_map[mid] = delta
        logger.info("  breakpoint %s: resolved %d nodes", name, len(resolved))
    return per_bp, hover_map


async def capture_full_dom(
    page,
    breakpoints: List[Dict[str, object]],
    *,
    base_url: str = "",
    cap: int = 9000,
) -> Tuple[Optional[ComponentNode], List[str], List[str], Dict[str, str], int]:
    """Return (tree, font_faces, keyframes, root_custom_properties, styled_count).

    Authored CSS is resolved at EACH breakpoint by resizing the page, because
    CDP getMatchedStylesForNode only returns viewport-active media rules.
    """
    from app.services.page_analyzer import to_modlix
    from app.services.page_analyzer.css_cdp import CDPStyleExtractor

    spec = await page.evaluate(_FULL_DOM_JS)
    faces_raw = await page.evaluate(_FONTFACE_JS)
    keyframes = await page.evaluate(_KEYFRAMES_JS)
    faces = await embed_font_faces(faces_raw or [], base_url)

    cdp = await page.context.new_cdp_session(page)
    extractor = CDPStyleExtractor(cdp)
    await extractor.enable()
    root_vars = await extractor.root_custom_properties()

    specs: List[Dict[str, Any]] = []
    _flatten_spec(spec, specs)
    all_ids = [s["id"] for s in specs]
    interactive_ids = {s["id"] for s in specs if s.get("tag") in INTERACTIVE_TAGS}

    per_bp, hover_map = await _resolve_all_breakpoints(
        extractor, page, breakpoints, all_ids, interactive_ids, cap
    )

    try:
        from app.agents.appbuilder.tools.modlix._conventions import (
            coerce_style_properties as _coerce,
        )
    except Exception:  # noqa: BLE001
        _coerce = None

    names = [str(b["name"]) for b in breakpoints]
    desktop_m = per_bp.get(names[0], {}) if names else {}
    tablet_m = per_bp.get(names[1], {}) if len(names) > 1 else {}
    mobile_m = per_bp.get(names[2], {}) if len(names) > 2 else {}

    def build(node_spec: Dict[str, Any]) -> ComponentNode:
        mid = node_spec["id"]
        node = ComponentNode(
            mxa_id=mid,
            component_type="",
            tag=node_spec.get("tag", ""),
            text=node_spec.get("text"),
            href=node_spec.get("href"),
            src=node_spec.get("src"),
            alt=node_spec.get("alt"),
            input_type=node_spec.get("type"),
            svg_html=node_spec.get("svg"),
        )
        sp = to_modlix.build_style_properties(
            desktop_m.get(mid), tablet_m.get(mid), mobile_m.get(mid),
            hover=hover_map.get(mid),
        )
        if _coerce is not None and sp:
            coerced, verr = _coerce(sp)
            if not verr and coerced:
                sp = coerced
        node.style_properties = sp
        for child in node_spec.get("children") or []:
            node.children.append(build(child))
        return node

    root = build(spec)
    styled = len(desktop_m)
    logger.info(
        "full DOM: styled %d nodes, %d font-faces, %d keyframes",
        styled, len(faces), len(keyframes or []),
    )
    return root, faces, list(keyframes or []), root_vars, styled
