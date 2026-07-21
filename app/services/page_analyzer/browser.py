"""Playwright orchestration for the page analyzer.

M1 scope: load the page once, stamp `data-mxa-id` on every body element, then
resize the SAME page across desktop/tablet/mobile and observe how each stamped
element reacts (visibility + bounding box) plus which `@media` queries are
active. Stamping on the live DOM and resizing (rather than reloading) keeps
element identity guaranteed across breakpoints.

Later milestones extend this module (CDP authored-CSS, sections, screenshots).
"""

from __future__ import annotations

import logging
import re
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse

from app.services.page_analyzer.models import (
    BreakpointInfo,
    NodeBreakpoint,
    NodeObservation,
    PageAnalysis,
    Rect,
)

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# (name, width, height). Desktop is first and is the canonical stamping pass.
DEFAULT_BREAKPOINTS: List[Dict[str, object]] = [
    {"name": "desktop", "width": 1440, "height": 900},
    {"name": "tablet", "width": 768, "height": 1024},
    {"name": "mobile", "width": 375, "height": 812},
]


# ── In-browser scripts ───────────────────────────────────────────────────

# Stamp every rendered element (body + descendants) with a stable id in
# document order (parents before children, so parent ids resolve). Returns the
# manifest so we keep tag/parent even if a later resize re-renders the DOM.
_STAMP_JS = r"""
() => {
  let i = 0;
  const manifest = [];
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE']);
  const stamp = (el) => {
    const id = 'm' + (i++);
    el.setAttribute('data-mxa-id', id);
    manifest.push({
      id,
      tag: el.tagName.toLowerCase(),
      parent: el.parentElement ? (el.parentElement.getAttribute('data-mxa-id') || null) : null,
    });
  };
  stamp(document.body);
  for (const el of document.body.querySelectorAll('*')) {
    if (SKIP.has(el.tagName)) continue;
    stamp(el);
  }
  return manifest;
}
"""

# Observe every stamped element at the current viewport + collect active media.
_OBSERVE_JS = r"""
() => {
  const nodes = [];
  for (const el of document.querySelectorAll('[data-mxa-id]')) {
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    const visible = !(
      s.display === 'none' || s.visibility === 'hidden' ||
      parseFloat(s.opacity || '1') === 0 || r.width < 1 || r.height < 1
    );
    nodes.push({
      id: el.getAttribute('data-mxa-id'),
      visible,
      rect: visible ? {
        x: Math.round(r.left + window.scrollX),
        y: Math.round(r.top + window.scrollY),
        w: Math.round(r.width),
        h: Math.round(r.height),
      } : null,
    });
  }
  // Best-effort media-query collection (cross-origin sheets are skipped here;
  // CDP picks them up in M2).
  const medias = new Set();
  for (const sheet of document.styleSheets) {
    let rules = null;
    try { rules = sheet.cssRules; } catch (e) { rules = null; }
    if (!rules) continue;
    for (const rule of rules) {
      if (rule.type === 4 /* CSSMediaRule */ && rule.media && rule.media.mediaText) {
        medias.add(rule.media.mediaText);
      }
    }
  }
  const active = [];
  for (const m of medias) {
    try { if (window.matchMedia(m).matches) active.push(m); } catch (e) {}
  }
  return { nodes, active_media: active.slice(0, 80) };
}
"""

_LAZY_LOAD_JS = (
    "() => new Promise((res) => { let y = 0;"
    " const step = () => {"
    "  window.scrollTo(0, y); y += 600;"
    "  if (y < document.documentElement.scrollHeight) setTimeout(step, 60);"
    "  else { window.scrollTo(0, 0); setTimeout(res, 400); }"
    " }; step(); })"
)


def handle_slug(url: str) -> str:
    """Stable filesystem slug for a URL (mirrors extract_site_assets)."""
    u = urlparse(url)
    host = re.sub(r"[^a-z0-9]+", "-", (u.netloc or "host").lower()).strip("-") or "host"
    path = re.sub(r"[^a-z0-9]+", "-", (u.path or "/").lower()).strip("-") or "root"
    return f"{host}__{path}"


async def _trigger_lazy_loads(page) -> None:
    try:
        await page.evaluate(_LAZY_LOAD_JS)
    except Exception:  # noqa: BLE001
        pass


async def observe_breakpoints(
    url: str,
    *,
    breakpoints: Optional[List[Dict[str, object]]] = None,
    headless: bool = True,
    wait_ms: int = 2500,
) -> PageAnalysis:
    """M1: stamp once, resize across breakpoints, observe each element.

    Returns a PageAnalysis with `observations` (per-element per-breakpoint
    visibility + box) and per-breakpoint summaries (counts + active media).
    """
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "playwright is not installed; run `python -m playwright install chromium`"
        ) from exc

    bps = breakpoints or DEFAULT_BREAKPOINTS
    first = bps[0]
    warnings: List[str] = []

    # manifest: ordered list of {id, tag, parent}
    manifest: List[Dict[str, Optional[str]]] = []
    # observations keyed by id -> NodeObservation
    obs_by_id: Dict[str, NodeObservation] = {}
    bp_infos: List[BreakpointInfo] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(
            viewport={"width": int(first["width"]), "height": int(first["height"])},
            ignore_https_errors=True,
            user_agent=_USER_AGENT,
        )
        page = await ctx.new_page()
        try:
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"goto did not reach networkidle: {exc}")
            await page.wait_for_timeout(wait_ms)
            await _trigger_lazy_loads(page)

            # Stamp once at the desktop viewport.
            manifest = await page.evaluate(_STAMP_JS)
            for entry in manifest:
                obs_by_id[entry["id"]] = NodeObservation(
                    mxa_id=entry["id"],
                    tag=entry["tag"] or "",
                    parent_mxa_id=entry["parent"],
                )
            logger.info("Stamped %d elements", len(manifest))

            for bp in bps:
                name = str(bp["name"])
                await page.set_viewport_size(
                    {"width": int(bp["width"]), "height": int(bp["height"])}
                )
                await page.wait_for_timeout(400)
                await _trigger_lazy_loads(page)

                result = await page.evaluate(_OBSERVE_JS)
                nodes = result.get("nodes", [])
                active_media = result.get("active_media", [])

                observed = 0
                visible = 0
                seen_ids = set()
                for n in nodes:
                    nid = n.get("id")
                    if nid is None:
                        continue
                    seen_ids.add(nid)
                    observed += 1
                    is_vis = bool(n.get("visible"))
                    if is_vis:
                        visible += 1
                    rect = None
                    if n.get("rect"):
                        r = n["rect"]
                        rect = Rect(x=r["x"], y=r["y"], w=r["w"], h=r["h"])
                    node_obs = obs_by_id.get(nid)
                    if node_obs is None:
                        # Element appeared only at this breakpoint (responsive
                        # insert, or DOM was re-rendered after resize).
                        node_obs = NodeObservation(mxa_id=nid, tag="")
                        obs_by_id[nid] = node_obs
                    node_obs.breakpoints.append(
                        NodeBreakpoint(breakpoint=name, visible=is_vis, rect=rect)
                    )

                # Mark elements that vanished from the DOM at this breakpoint.
                for nid, node_obs in obs_by_id.items():
                    if nid not in seen_ids and not any(
                        b.breakpoint == name for b in node_obs.breakpoints
                    ):
                        node_obs.breakpoints.append(
                            NodeBreakpoint(breakpoint=name, visible=False, rect=None)
                        )

                if manifest and observed < len(manifest) * 0.5:
                    warnings.append(
                        f"breakpoint '{name}': only {observed}/{len(manifest)} stamped "
                        "elements found (DOM likely re-rendered on resize)"
                    )

                bp_infos.append(
                    BreakpointInfo(
                        name=name,
                        width=int(bp["width"]),
                        height=int(bp["height"]),
                        observed_count=observed,
                        visible_count=visible,
                        active_media=active_media,
                    )
                )
                logger.info(
                    "breakpoint %s: observed=%d visible=%d active_media=%d",
                    name,
                    observed,
                    visible,
                    len(active_media),
                )
        finally:
            await browser.close()

    return PageAnalysis(
        url=url,
        analyzed_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        stage="m1",
        breakpoints=bp_infos,
        total_elements=len(manifest),
        observations=list(obs_by_id.values()),
        warnings=warnings,
    )


# Find a few representative elements to validate authored-CSS extraction (M2).
_FIND_TARGETS_JS = r"""
() => {
  const out = {};
  const vis = (el) => {
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return !(s.display === 'none' || s.visibility === 'hidden' || r.width < 1 || r.height < 1);
  };
  let best = null, area = -1;
  for (const el of document.querySelectorAll('h1, h2')) {
    if (!vis(el)) continue;
    const r = el.getBoundingClientRect();
    const a = r.width * r.height;
    if (a > area) { area = a; best = el; }
  }
  if (best) out.hero_heading = best.getAttribute('data-mxa-id');
  for (const sel of ['button', 'a', 'p']) {
    for (const el of document.querySelectorAll(sel)) {
      if (vis(el)) {
        out[sel === 'button' ? 'button' : (sel === 'a' ? 'link' : 'paragraph')] =
          el.getAttribute('data-mxa-id');
        break;
      }
    }
  }
  return out;
}
"""


async def extract_authored_sample(
    url: str,
    *,
    headless: bool = True,
    wait_ms: int = 2500,
    widths: Optional[Dict[str, int]] = None,
) -> Dict[str, object]:
    """M2: stamp, then extract AUTHORED CSS for a few representative elements via
    CDP and convert to Modlix styleProperties. Validates the extraction +
    media-bucketing + hover + Modlix shape end-to-end on a real page.
    """
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("playwright not installed") from exc

    from app.services.page_analyzer import to_modlix
    from app.services.page_analyzer.css_cdp import CDPStyleExtractor

    widths = widths or {"desktop": 1440, "tablet": 768, "mobile": 375}
    desktop_w = widths.get("desktop", 1440)

    results: List[Dict[str, object]] = []
    root_vars: Dict[str, str] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(
            viewport={"width": desktop_w, "height": 900},
            ignore_https_errors=True,
            user_agent=_USER_AGENT,
        )
        page = await ctx.new_page()
        try:
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(wait_ms)
            await _trigger_lazy_loads(page)

            await page.evaluate(_STAMP_JS)
            targets = await page.evaluate(_FIND_TARGETS_JS)

            cdp = await page.context.new_cdp_session(page)
            extractor = CDPStyleExtractor(cdp)
            await extractor.enable()

            for role, mxa_id in targets.items():
                if not mxa_id:
                    continue
                raw = await extractor.extract(mxa_id, widths)
                if not raw:
                    continue
                per = raw["per_breakpoint"]
                style_props = to_modlix.build_style_properties(
                    per.get("desktop"),
                    per.get("tablet"),
                    per.get("mobile"),
                    hover=raw.get("hover"),
                )
                results.append(
                    {
                        "role": role,
                        "mxa_id": mxa_id,
                        "authored": per,
                        "hover": raw.get("hover"),
                        "style_properties": style_props,
                    }
                )

            root_vars = await extractor.root_custom_properties()
        finally:
            await browser.close()

    return {"url": url, "widths": widths, "targets": results, "root_custom_properties": root_vars}


async def _capture_section_shots(page, segments, shots_dir, bp_name, warnings):
    """Screenshot each section (by data-mxa-section) + a full-page shot."""
    import os

    bp_dir = os.path.join(shots_dir, bp_name)
    os.makedirs(bp_dir, exist_ok=True)
    out: Dict[int, str] = {}
    full_rel = None
    try:
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(150)
        full_path = os.path.join(bp_dir, "fullpage.png")
        await page.screenshot(path=full_path, full_page=True, type="png")
        full_rel = os.path.relpath(full_path, shots_dir)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"fullpage shot failed @{bp_name}: {exc}")

    for seg in segments:
        idx = seg.get("index", 0)
        role = seg.get("role", "content")
        path = os.path.join(bp_dir, f"section-{idx:02d}-{role}.png")
        try:
            loc = page.locator(f'[data-mxa-section="{idx}"]').first
            await loc.scroll_into_view_if_needed(timeout=3000)
            await loc.screenshot(path=path, type="png", timeout=10000)
            out[idx] = os.path.relpath(path, shots_dir)
        except Exception:  # noqa: BLE001
            try:  # fallback: viewport snap at the section's top
                y = max(0, int((seg.get("rect") or {}).get("y", 0)) - 20)
                await page.evaluate(f"window.scrollTo(0, {y})")
                await page.wait_for_timeout(150)
                await page.screenshot(path=path, full_page=False, type="png")
                out[idx] = os.path.relpath(path, shots_dir)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"section {idx} shot failed @{bp_name}: {exc}")
    return out, full_rel


async def _attach_authored_styles(page, node_map, widths, style_cap, warnings):
    """Attach Modlix styleProperties (authored CSS via CDP) to each node.
    Returns the :root custom properties. Done once at the desktop viewport
    (media buckets are derived in Python, so resizing is unnecessary)."""
    from app.services.page_analyzer import to_modlix
    from app.services.page_analyzer.css_cdp import CDPStyleExtractor

    cdp = await page.context.new_cdp_session(page)
    extractor = CDPStyleExtractor(cdp)
    await extractor.enable()

    ids = list(node_map.keys())
    if len(ids) > style_cap:
        warnings.append(
            f"styled {style_cap}/{len(ids)} nodes (style_cap); rest have no styleProperties"
        )
        ids = ids[:style_cap]

    try:
        from app.agents.appbuilder.tools.modlix._conventions import (
            coerce_style_properties as _coerce,
        )
    except Exception:  # noqa: BLE001
        _coerce = None

    invalid = 0
    for mid in ids:
        raw = await extractor.extract(mid, widths)
        if not raw:
            continue
        per = raw["per_breakpoint"]
        sp = to_modlix.build_style_properties(
            per.get("desktop"), per.get("tablet"), per.get("mobile"), hover=raw.get("hover")
        )
        if _coerce is not None and sp:
            coerced, verr = _coerce(sp)
            if verr or not coerced:
                invalid += 1
            else:
                sp = coerced  # platform-canonical form
        node_map[mid].style_properties = sp

    if invalid:
        warnings.append(f"{invalid} nodes' styleProperties were rejected by coerce_style_properties")
    return await extractor.root_custom_properties()


async def _attach_visibility_and_shots(
    page, bps, node_map, sections, segments, shots_dir, with_visibility, with_shots, warnings
):
    """Resize across breakpoints; record per-node visibility and/or capture
    per-section screenshots (same page, attributes persist)."""
    for bp in bps:
        name = str(bp["name"])
        await page.set_viewport_size({"width": int(bp["width"]), "height": int(bp["height"])})
        await page.wait_for_timeout(400)
        await _trigger_lazy_loads(page)
        if with_visibility:
            result = await page.evaluate(_OBSERVE_JS)
            for n in result.get("nodes", []):
                nid = n.get("id")
                if nid in node_map:
                    node_map[nid].visibility[name] = bool(n.get("visible"))
        if with_shots and shots_dir:
            shots, _full = await _capture_section_shots(page, segments, shots_dir, name, warnings)
            for seg in sections:
                if seg.index in shots:
                    seg.screenshots[name] = shots[seg.index]


async def run_pipeline(
    url: str,
    *,
    headless: bool = True,
    wait_ms: int = 2500,
    breakpoints: Optional[List[Dict[str, object]]] = None,
    with_styles: bool = False,
    with_visibility: bool = False,
    with_shots: bool = False,
    shots_dir: Optional[str] = None,
    use_llm: bool = False,
    style_cap: int = 2500,
    stage: str = "m3",
) -> PageAnalysis:
    """Unified pipeline: dismiss banner -> stamp -> segment -> walk -> build tree,
    then optionally attach authored styles (CDP), per-breakpoint visibility, and
    per-section screenshots. M3/M4/M5 toggle the flags."""
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("playwright not installed") from exc

    from app.services.page_analyzer.banner import dismiss_banner
    from app.services.page_analyzer.segment import (
        _SEGMENT_JS,
        _WALK_JS,
        build_sections,
        count_nodes,
        iter_nodes,
    )

    bps = breakpoints or DEFAULT_BREAKPOINTS
    widths = {str(b["name"]): int(b["width"]) for b in bps}
    warnings: List[str] = []
    banner_info: Dict[str, object] = {}
    root_vars: Dict[str, str] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(
            viewport={"width": int(bps[0]["width"]), "height": int(bps[0]["height"])},
            ignore_https_errors=True,
            user_agent=_USER_AGENT,
        )
        page = await ctx.new_page()
        try:
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"goto did not reach networkidle: {exc}")
            await page.wait_for_timeout(wait_ms)
            banner_info = await dismiss_banner(page, use_llm=use_llm)
            await _trigger_lazy_loads(page)

            await page.evaluate(_STAMP_JS)
            segments = await page.evaluate(_SEGMENT_JS)
            section_ids = [s["mxa_id"] for s in segments if s.get("mxa_id")]
            kept = await page.evaluate(_WALK_JS, section_ids)

            sections = build_sections(segments, kept)
            node_map = {n.mxa_id: n for n in iter_nodes(sections)}

            if with_styles:
                root_vars = await _attach_authored_styles(
                    page, node_map, widths, style_cap, warnings
                )

            if with_visibility or with_shots:
                await _attach_visibility_and_shots(
                    page, bps, node_map, sections, segments, shots_dir,
                    with_visibility, with_shots, warnings,
                )
        finally:
            await browser.close()

    logger.info(
        "sections=%d kept_nodes=%d tree_nodes=%d banner=%s",
        len(sections),
        len(kept),
        count_nodes(sections),
        banner_info.get("method"),
    )
    return PageAnalysis(
        url=url,
        analyzed_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        stage=stage,
        total_elements=len(kept),
        sections=sections,
        root_custom_properties=root_vars,
        warnings=warnings,
        extra={"banner": banner_info},
    )


async def analyze_structure(
    url: str,
    *,
    headless: bool = True,
    wait_ms: int = 2500,
) -> PageAnalysis:
    """M3: structure only (sections + classified trees), no styles/shots."""
    return await run_pipeline(url, headless=headless, wait_ms=wait_ms, stage="m3")


async def run_full_dom(
    url: str,
    *,
    headless: bool = True,
    wait_ms: int = 2500,
    use_llm: bool = False,
    breakpoints: Optional[List[Dict[str, object]]] = None,
) -> PageAnalysis:
    """Full-DOM capture for faithful render: every visible element + authored CSS."""
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("playwright not installed") from exc

    from app.services.page_analyzer.banner import dismiss_banner
    from app.services.page_analyzer.full_dom import capture_full_dom

    bps = breakpoints or DEFAULT_BREAKPOINTS
    warnings: List[str] = []
    banner_info: Dict[str, object] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(
            viewport={"width": int(bps[0]["width"]), "height": int(bps[0]["height"])},
            ignore_https_errors=True,
            user_agent=_USER_AGENT,
        )
        page = await ctx.new_page()
        try:
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"goto did not reach networkidle: {exc}")
            await page.wait_for_timeout(wait_ms)
            banner_info = await dismiss_banner(page, use_llm=use_llm)
            await _trigger_lazy_loads(page)
            root, faces, keyframes, root_vars, styled = await capture_full_dom(
                page, bps, base_url=url
            )
        finally:
            await browser.close()

    logger.info("full DOM: styled=%d font_faces=%d banner=%s", styled, len(faces), banner_info.get("method"))
    return PageAnalysis(
        url=url,
        analyzed_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        stage="full",
        total_elements=styled,
        full_tree=root,
        font_faces=faces,
        keyframes=keyframes,
        root_custom_properties=root_vars,
        warnings=warnings,
        extra={"banner": banner_info},
    )
