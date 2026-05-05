"""Hybrid clone — screenshot visual layout + Playwright DOM images.

Combines the best of both pipelines:
- Screenshot pipeline: LLM understands visual layout, sections, spacing
- URL pipeline: Playwright extracts REAL image URLs from the DOM

Flow:
1. Playwright loads URL, takes full-page screenshot
2. Playwright extracts all <img> srcs + background-image URLs per section
3. LLM section discovery on screenshot
4. Per-section: gpt-4o gets screenshot + list of real image URLs from DOM
5. gpt-4o generates Modlix JSON using visual layout + real image srcs
6. Images downloaded + re-uploaded to files service
7. Save page
8. QA: compare, iterate if < target
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import uuid
from typing import Any

from PIL import Image

from app.core.tools.base import (
    ToolDefinition,
    ToolParameter,
    ToolResult,
    ResultTier,
)

logger = logging.getLogger(__name__)


async def _extract_images_from_dom(url: str) -> dict[str, list[dict]]:
    """Use Playwright to extract all image URLs per section from the DOM.

    Returns dict of section_index → list of {src, alt, width, height, y}.
    Also returns the full-page screenshot as base64.
    """
    from playwright.async_api import async_playwright

    result = {"screenshot_b64": "", "page_images": [], "page_height": 0, "page_width": 0}

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1440, "height": 900})
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(4)

            # Full-page screenshot
            screenshot = await page.screenshot(full_page=True)
            result["screenshot_b64"] = base64.b64encode(screenshot).decode("ascii")

            # Extract all images from DOM (unwrap CDN proxy URLs in-browser)
            images = await page.evaluate("""() => {
                // Unwrap CDN proxy URLs like speedsize.com/UUID/https://real-url
                function unwrapCdn(url) {
                    // Pattern: https://cdn.speedsize.com/<uuid>/https://real-host/path
                    const m = url.match(/https?:\\/\\/[^/]*speedsize\\.com\\/[^/]+\\/(https?:\\/\\/.+)/);
                    if (m) return { src: m[1], cdnSrc: url };
                    // Pattern: https://cdn.imgproxy.com/.../<base64-or-url>/...
                    // Pattern: https://images.weserv.nl/?url=<encoded>
                    const w = url.match(/[?&]url=([^&]+)/);
                    if (w) {
                        try { return { src: decodeURIComponent(w[1]), cdnSrc: url }; }
                        catch(e) {}
                    }
                    return { src: url, cdnSrc: null };
                }

                const imgs = [];
                // <img> tags
                for (const img of document.querySelectorAll('img')) {
                    const rect = img.getBoundingClientRect();
                    if (rect.width < 5 || rect.height < 5) continue;
                    const raw = img.currentSrc || img.src || img.dataset?.src || '';
                    if (!raw || raw.startsWith('data:')) continue;
                    const { src, cdnSrc } = unwrapCdn(raw);
                    imgs.push({
                        src, cdnSrc: cdnSrc || undefined,
                        alt: img.alt || '',
                        width: Math.round(rect.width), height: Math.round(rect.height),
                        x: Math.round(rect.x), y: Math.round(rect.y + window.scrollY),
                        type: 'img'
                    });
                }
                // background-image URLs
                for (const el of document.querySelectorAll('*')) {
                    const bg = getComputedStyle(el).backgroundImage;
                    if (!bg || bg === 'none' || bg.startsWith('linear-gradient') || bg.startsWith('radial-gradient')) continue;
                    const m = bg.match(/url\\(['"]?(https?:\\/\\/[^'"\\)]+)['"]?\\)/);
                    if (!m) continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 20 || rect.height < 20) continue;
                    const { src, cdnSrc } = unwrapCdn(m[1]);
                    imgs.push({
                        src, cdnSrc: cdnSrc || undefined,
                        alt: '',
                        width: Math.round(rect.width), height: Math.round(rect.height),
                        x: Math.round(rect.x), y: Math.round(rect.y + window.scrollY),
                        type: 'bg'
                    });
                }
                return {
                    images: imgs,
                    height: document.documentElement.scrollHeight,
                    width: document.documentElement.scrollWidth,
                };
            }""")

            result["page_images"] = images.get("images", [])
            result["page_height"] = images.get("height", 900)
            result["page_width"] = images.get("width", 1440)

            # Extract top-level section computed styles for precise CSS values
            sections_css = await page.evaluate("""() => {
                // Find major sections: direct children of body/main or semantic tags
                const body = document.body;
                const candidates = [];
                // Try semantic sections first
                for (const tag of ['header', 'nav', 'main', 'section', 'footer', 'article']) {
                    for (const el of document.querySelectorAll(tag)) {
                        const rect = el.getBoundingClientRect();
                        if (rect.height > 50 && rect.width > 200) {
                            candidates.push(el);
                        }
                    }
                }
                // Also try direct children of body with significant height
                for (const el of body.children) {
                    const rect = el.getBoundingClientRect();
                    if (rect.height > 100 && rect.width > 200 && !candidates.includes(el)) {
                        candidates.push(el);
                    }
                }
                // Deduplicate overlapping regions, sort by y
                candidates.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);

                return candidates.slice(0, 30).map(el => {
                    const rect = el.getBoundingClientRect();
                    const cs = getComputedStyle(el);
                    return {
                        tag: el.tagName.toLowerCase(),
                        y: Math.round(rect.y + window.scrollY),
                        height: Math.round(rect.height),
                        width: Math.round(rect.width),
                        backgroundColor: cs.backgroundColor,
                        color: cs.color,
                        padding: cs.padding,
                        display: cs.display,
                        flexDirection: cs.flexDirection,
                        justifyContent: cs.justifyContent,
                        alignItems: cs.alignItems,
                        gap: cs.gap,
                        maxWidth: cs.maxWidth,
                        fontFamily: cs.fontFamily?.split(',')[0]?.trim()?.replace(/['"]/g, '') || '',
                        fontSize: cs.fontSize,
                        textAlign: cs.textAlign,
                        backgroundImage: cs.backgroundImage !== 'none' ? cs.backgroundImage.substring(0, 200) : '',
                    };
                });
            }""")
            result["sections_css"] = sections_css or []
            logger.info("DOM sections CSS: %d sections extracted", len(result.get("sections_css", [])))

            await browser.close()
    except Exception as e:
        logger.error("DOM extraction failed: %s", e)

    logger.info("DOM extraction: %d images, page %dx%d",
                len(result["page_images"]), result["page_width"], result["page_height"])
    return result


async def _execute_hybrid_clone(
    params: dict[str, Any],
    context: dict[str, Any],
) -> ToolResult:
    """Hybrid clone: screenshot layout + DOM images."""
    url = params.get("url", "")
    page_name = params.get("page_name", "hybrid").lower()
    app_code = params.get("app_code") or context.get("app_code", "")

    if not url:
        return ToolResult(success=False, error="url is required.")
    if not app_code:
        return ToolResult(success=False, error="app_code is required.")

    client_code = context.get("client_code", "SYSTEM")
    headers = context["headers"]

    # ── Step 0: Modlix site detection ──
    from app.agents.appbuilder.tools._shared import detect_modlix_site
    from app.agents.appbuilder.tools.clone_tool import _modlix_fast_clone

    logger.info("Step 0: Checking if %s is a Modlix site...", url)
    modlix_data = await detect_modlix_site(url)
    if modlix_data:
        logger.info("Modlix site detected! Fast-path clone of '%s' (%d components)",
                     modlix_data.page_name, len(modlix_data.component_definition))
        result = await _modlix_fast_clone(
            modlix_data, page_name, app_code, client_code, headers,
            True, url,
        )
        if result is not None:
            return result
        logger.info("Modlix fast-clone returned None, falling through to hybrid pipeline")

    # ── Step 1: Playwright → screenshot + DOM images ──
    logger.info("Step 1: Extracting from %s via Playwright...", url)
    dom_data = await _extract_images_from_dom(url)

    screenshot_b64 = dom_data["screenshot_b64"]
    if not screenshot_b64:
        return ToolResult(success=False, error=f"Could not load {url}")

    page_images = dom_data["page_images"]
    page_h = dom_data["page_height"]
    sections_css = dom_data.get("sections_css", [])
    logger.info("Step 1: Got screenshot + %d DOM images + %d DOM section styles",
                len(page_images), len(sections_css))

    # ── Step 2: Run the screenshot pipeline with DOM image URLs injected ──
    # Store screenshot in session context (screenshot_tool reads from there)
    session_ctx = context.get("session_context", {})
    session_ctx["user_screenshot_b64"] = screenshot_b64
    session_ctx["user_screenshot_mime"] = "image/png"

    # Group DOM images by approximate y-position (will be matched to sections later)
    # For now, pass ALL image URLs to each section prompt — gpt-4o will pick the right ones
    image_list_text = "\n".join(
        f"- {img['src'][:100]} (w={img['width']}, h={img['height']}, y={img['y']}, alt=\"{img['alt'][:30]}\")"
        for img in page_images[:50]  # Cap at 50 to fit in prompt
    )

    # Import and run the screenshot pipeline
    from app.agents.appbuilder.tools.screenshot_tool import (
        _phase0_scan, _phase2_build_slice, _validate_components,
        _SECTION_DISCOVERY_PROMPT, _vision_to_json,
        _is_dark_color, _is_light_color,
    )
    from app.agents.appbuilder.tools.screenshot_slicer import SliceSpec
    from app.agents.appbuilder.tools.asset_resolver import (
        resolve_assets, detect_icon_packs, rewrite_placeholders,
        _upload_to_files, _download_url,
    )
    from app.agents.appbuilder.tools._shared import get_saas_client
    from app.agents.appbuilder.tools.html_to_modlix import _extract_font_packs

    # Decode screenshot
    img_bytes = base64.b64decode(screenshot_b64)
    full_img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    logger.info("Screenshot: %dx%d", full_img.width, full_img.height)

    # Phase 0: page scan
    max_dim = 1568
    thumb = full_img.copy()
    if max(thumb.size) > max_dim:
        ratio = max_dim / max(thumb.size)
        thumb = thumb.resize((int(thumb.width * ratio), int(thumb.height * ratio)), Image.LANCZOS)
    thumb_buf = io.BytesIO()
    thumb.save(thumb_buf, format="JPEG", quality=80)
    thumb_b64 = base64.b64encode(thumb_buf.getvalue()).decode("ascii")

    try:
        page_info = await _phase0_scan(thumb_b64)
    except Exception:
        page_info = {"body_font": "sans-serif", "body_color": "#000000", "body_bg": "#ffffff"}

    # Phase 1: LLM section discovery
    logger.info("Step 2: LLM section discovery...")
    try:
        section_result = await _vision_to_json(thumb_b64, _SECTION_DISCOVERY_PROMPT.format(
            width=full_img.width, height=full_img.height,
        ), max_tokens=2048)
        cuts = section_result.get("sections", [])
    except Exception:
        cuts = None

    if not cuts or len(cuts) < 2:
        from app.agents.appbuilder.tools.screenshot_slicer import slice_screenshot
        slices = slice_screenshot(screenshot_b64)
    else:
        import numpy as np
        slices = []
        for i, sec in enumerate(cuts):
            y_start_pct = sec.get("y_start_pct", 0)
            y_end_pct = sec.get("y_end_pct", 100)
            y_start_full = round(full_img.height * y_start_pct / 100)
            y_end_full = min(round(full_img.height * y_end_pct / 100), full_img.height)
            if y_end_full <= y_start_full:
                continue
            crop = full_img.crop((0, y_start_full, full_img.width, y_end_full))
            buf = io.BytesIO()
            crop.save(buf, format="JPEG", quality=85)
            jpeg_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            arr = np.array(crop)
            samples = [arr[0, 0], arr[0, -1], arr[-1, 0], arr[-1, -1], arr[arr.shape[0] // 2, arr.shape[1] // 2]]
            avg = np.mean(samples, axis=0).astype(int)
            avg_color = f"#{avg[0]:02x}{avg[1]:02x}{avg[2]:02x}"

            slices.append(SliceSpec(
                index=i, y_start=y_start_full, y_end=y_end_full,
                height=y_end_full - y_start_full, width=full_img.width,
                jpeg_b64=jpeg_b64, avg_bg_color=avg_color,
            ))

    logger.info("Step 2: %d sections identified", len(slices))

    # Phase 2: Per-section gpt-4o with REAL image URLs
    # Enhance the prompt with DOM image URLs for each section
    _HYBRID_EXTRA = """
## OVERRIDE — REAL IMAGE URLs (from the DOM)
IMPORTANT: Ignore the "Image Placeholders" rule above. DO NOT leave src empty.
Instead, use the REAL image URLs listed below for every Image component.

The following images were extracted from the live DOM of this section.
The width/height values show how large each image appears on the actual page:
{image_urls}

### Image sizing rules (CRITICAL for visual fidelity):
- If an image spans the FULL WIDTH of the section (w >= 1200px), set its styles to:
  width: "100%", height: "auto", objectFit: "cover"
  This makes it a full-bleed hero/banner image matching the source layout.
- If an image is a BACKGROUND for a section (large image behind text), make it a
  child of the section Grid with position: "absolute", top: "0", left: "0",
  width: "100%", height: "100%", objectFit: "cover", zIndex: "0".
  Then put text/buttons on top with position: "relative", zIndex: "1".
- For medium images (300-1200px wide), use the EXACT width from the DOM data above.
- For small images/icons/logos (< 300px), use precise dimensions from the DOM data.

### How to match URLs to visible elements:
1. Pick the URL whose DOM dimensions (w, h) and alt text best match what you see
2. Use the EXACT full URL string (it will be re-uploaded later)
3. If the section has a LARGE photo as a background, that's the full-width image
4. Do NOT shrink large images — they should fill their container
"""

    sem = asyncio.Semaphore(3)
    comp_def: dict[str, Any] = {}
    all_assets: list[dict] = []
    all_fonts: list[dict] = []
    section_keys: list[str] = []

    async def build_section(sl: SliceSpec):
        async with sem:
            # Find DOM images overlapping this slice's y-range.
            # Use generous padding (200px) because LLM section boundaries
            # don't perfectly align with DOM element positions, and images
            # near a boundary are usually part of the section.
            pad = 200
            section_imgs = [
                img for img in page_images
                if (img["y"] + img.get("height", 0)) >= (sl.y_start - pad)
                and img["y"] <= (sl.y_end + pad)
                and img.get("width", 0) > 20  # skip tiny icons
            ]
            # Deduplicate by src (same image can appear in overlapping ranges)
            seen_srcs = set()
            unique_imgs = []
            for img in section_imgs:
                if img["src"] not in seen_srcs:
                    seen_srcs.add(img["src"])
                    unique_imgs.append(img)
            section_imgs = unique_imgs

            img_urls_text = "\n".join(
                f"- {img['src']} (w={img['width']}px, h={img['height']}px, alt=\"{img.get('alt','')[:40]}\")"
                for img in section_imgs[:20]
            ) or "No images found in this section."

            # Detect image clusters — multiple images at similar y-positions = grid/carousel
            grid_hint = ""
            if len(section_imgs) >= 2:
                # Group by y-position (within 50px tolerance)
                y_groups: dict[int, list] = {}
                for img in section_imgs:
                    bucket = round(img["y"] / 50) * 50
                    y_groups.setdefault(bucket, []).append(img)
                for bucket, grp in y_groups.items():
                    if len(grp) >= 2:
                        grid_hint += (
                            f"\n** GRID DETECTED: {len(grp)} images at similar y-position "
                            f"(~{bucket}px) — these are a PRODUCT GRID or CAROUSEL. "
                            f"Create {len(grp)} card components side-by-side in a ROWLAYOUT Grid. "
                            f"Each card should have its own Image + title Text. **"
                        )
            if grid_hint:
                img_urls_text += "\n" + grid_hint

            # Find DOM sections overlapping this slice → extract exact CSS
            css_hint = ""
            matching_css = [
                s for s in sections_css
                if s["y"] + s["height"] > sl.y_start and s["y"] < sl.y_end
                and s["height"] > 50
            ]
            if matching_css:
                css_lines = []
                for mc in matching_css[:3]:  # top 3 matching DOM sections
                    props = []
                    if mc.get("backgroundColor") and mc["backgroundColor"] != "rgba(0, 0, 0, 0)":
                        props.append(f"backgroundColor: {mc['backgroundColor']}")
                    if mc.get("color"):
                        props.append(f"color: {mc['color']}")
                    if mc.get("padding") and mc["padding"] != "0px":
                        props.append(f"padding: {mc['padding']}")
                    if mc.get("display"):
                        props.append(f"display: {mc['display']}")
                    if mc.get("flexDirection") and mc["flexDirection"] != "row":
                        props.append(f"flexDirection: {mc['flexDirection']}")
                    if mc.get("gap") and mc["gap"] != "normal":
                        props.append(f"gap: {mc['gap']}")
                    if mc.get("alignItems") and mc["alignItems"] != "normal":
                        props.append(f"alignItems: {mc['alignItems']}")
                    if mc.get("justifyContent") and mc["justifyContent"] != "normal":
                        props.append(f"justifyContent: {mc['justifyContent']}")
                    if mc.get("maxWidth") and mc["maxWidth"] != "none":
                        props.append(f"maxWidth: {mc['maxWidth']}")
                    if props:
                        css_lines.append(
                            f"  <{mc['tag']}> ({mc['width']}x{mc['height']}px): "
                            + ", ".join(props)
                        )
                if css_lines:
                    css_hint = (
                        "\n\n## EXACT CSS from DOM (use these values, don't guess!):\n"
                        + "\n".join(css_lines)
                    )

            logger.info("  Section %d: %d DOM images in y-range [%d-%d]%s%s",
                        sl.index, len(section_imgs), sl.y_start, sl.y_end,
                        " (grid detected)" if grid_hint else "",
                        f" ({len(matching_css)} DOM styles)" if matching_css else "")

            css_w = min(sl.width, 1440)
            css_h = round(sl.height * (1440 / sl.width)) if sl.width > 1440 else sl.height

            extra_prompt = _HYBRID_EXTRA.format(image_urls=img_urls_text)
            if css_hint:
                extra_prompt += css_hint

            return await _phase2_build_slice(
                sl.jpeg_b64, sl.index, len(slices),
                css_w, css_h, sl.avg_bg_color, {
                    **page_info,
                    "_extra_prompt": extra_prompt,
                },
            )

    logger.info("Step 3: Building %d sections with DOM images (3 concurrent)...", len(slices))
    results = await asyncio.gather(*[build_section(sl) for sl in slices], return_exceptions=True)

    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.warning("Section %d failed: %s", i, result)
            continue
        components = result.get("components", {})
        comp_def.update(components)
        all_assets.extend(result.get("assets", []))
        all_fonts.extend(result.get("fonts", []))
        root_key = f"s{i}_sectionRoot"
        if root_key in components:
            section_keys.append(root_key)
        else:
            for k in components:
                if k.startswith(f"s{i}_"):
                    section_keys.append(k)
                    break

    if not section_keys:
        return ToolResult(success=False, error="No sections built.")

    comp_def = _validate_components(comp_def)

    # Set section heights and ensure overflow/position for bg images
    for sl in slices:
        root_key = f"s{sl.index}_sectionRoot"
        if root_key not in comp_def:
            continue
        sp = comp_def[root_key].get("styleProperties", {})
        if sp:
            first_sid = next(iter(sp))
            all_res = sp[first_sid].setdefault("resolutions", {}).setdefault("ALL", {})
            scaled_h = round(sl.height * (1440 / sl.width))
            all_res["minHeight"] = {"value": f"{scaled_h}px"}
            all_res.setdefault("width", {"value": "100%"})
            all_res.setdefault("overflow", {"value": "hidden"})
            all_res.setdefault("position", {"value": "relative"})

    # Text color correction — use both pixel avg AND the LLM-set backgroundColor
    def _get_section_bg_luminance(root_comp: dict, fallback_color: str) -> float:
        """Get luminance from the section root's backgroundColor or pixel avg."""
        # First try the LLM-generated backgroundColor (more reliable than pixel avg)
        for sid, st in root_comp.get("styleProperties", {}).items():
            bgc = st.get("resolutions", {}).get("ALL", {}).get("backgroundColor", {})
            bgv = bgc.get("value", "") if isinstance(bgc, dict) else ""
            if bgv and bgv.startswith("#"):
                try:
                    h = bgv.lstrip("#")
                    if len(h) == 3:
                        h = "".join(c * 2 for c in h)
                    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
                    return 0.299 * r + 0.587 * g + 0.114 * b
                except Exception:
                    pass
        # Fallback to pixel average
        try:
            h = fallback_color.lstrip("#")
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            return 0.299 * r + 0.587 * g + 0.114 * b
        except Exception:
            return 128

    for sl in slices:
        root_key = f"s{sl.index}_sectionRoot"
        comp = comp_def.get(root_key)
        if not comp:
            continue
        lum = _get_section_bg_luminance(comp, sl.avg_bg_color)
        is_dark = lum < 128
        target = "#ffffff" if is_dark else "#111111"

        def _fix(key):
            c = comp_def.get(key)
            if not c:
                return
            if c.get("type") in ("Text", "Button"):
                for sid, st in c.get("styleProperties", {}).items():
                    ar = st.get("resolutions", {}).get("ALL", {})
                    cv = ar.get("color", {})
                    cvv = cv.get("value", "") if isinstance(cv, dict) else ""
                    if not cvv or (is_dark and _is_dark_color(cvv)) or (not is_dark and _is_light_color(cvv)):
                        ar["color"] = {"value": target}
            for ck in c.get("children", {}):
                _fix(ck)
        _fix(root_key)

    logger.info("Step 3: %d components, %d sections", len(comp_def), len(section_keys))

    # ── Step 4: Download + re-upload all external images ──
    # Build a lookup of unwrapped→CDN URL for fallback downloads.
    # The LLM receives unwrapped URLs (short, clean) but some origin servers
    # require the CDN wrapper for auth tokens / signed URLs.
    cdn_fallback: dict[str, str] = {}
    for img in page_images:
        if img.get("cdnSrc"):
            cdn_fallback[img["src"]] = img["cdnSrc"]

    api_client = get_saas_client()
    img_cache: dict[str, str] = {}
    rewrite_count = 0
    download_fail = 0

    import httpx
    async with httpx.AsyncClient(
        timeout=20, follow_redirects=True,
        headers={"Referer": url, "User-Agent": "Mozilla/5.0"},
    ) as dl_client:
        for key, comp in comp_def.items():
            if comp.get("type") != "Image":
                continue
            src = comp.get("properties", {}).get("src", {}).get("value", "")
            if not src or not src.startswith("http"):
                continue
            # Check cache first
            if src in img_cache:
                comp["properties"]["src"]["value"] = img_cache[src]
                rewrite_count += 1
                continue

            # Try downloading: first the unwrapped URL, then CDN fallback
            urls_to_try = [src]
            if src in cdn_fallback:
                urls_to_try.append(cdn_fallback[src])

            downloaded = False
            for try_url in urls_to_try:
                try:
                    resp = await dl_client.get(try_url)
                    if resp.status_code == 200 and len(resp.content) > 100:
                        ct = resp.headers.get("content-type", "image/png").split(";")[0].strip()
                        if "image" in ct or "svg" in ct or "octet" in ct:
                            uploaded = await _upload_to_files(
                                resp.content, ct, app_code, client_code,
                                api_client, headers, img_cache, page_name=page_name,
                            )
                            if uploaded:
                                img_cache[src] = uploaded
                                comp["properties"]["src"]["value"] = uploaded
                                rewrite_count += 1
                                downloaded = True
                                break
                except Exception as e:
                    logger.debug("Download attempt failed for %s: %s", try_url[:80], e)

            if not downloaded:
                download_fail += 1
                logger.debug("All download attempts failed for: %s", src[:100])

    logger.info("Step 4: Re-uploaded %d images (%d unique, %d failed)",
                rewrite_count, len(img_cache), download_fail)

    # Detect icon packs
    icon_packs = detect_icon_packs(comp_def)

    # ── Step 5: Assemble + save ──
    body_font = page_info.get("body_font", "")
    body_color = page_info.get("body_color", "")

    root_styles: dict[str, Any] = {
        "width": {"value": "100%"},
        "display": {"value": "flex"},
        "flexDirection": {"value": "column"},
        "alignItems": {"value": "center"},
    }
    if body_font:
        root_styles["fontFamily"] = {"value": body_font}
    if body_color:
        root_styles["color"] = {"value": body_color}

    style_id = uuid.uuid4().hex[:22]
    comp_def["root"] = {
        "key": "root", "name": "rootGrid", "type": "Grid",
        "properties": {"containerType": {"value": "_bare"}},
        "styleProperties": {style_id: {"resolutions": {"ALL": root_styles}}},
        "children": {k: True for k in section_keys},
        "displayOrder": 0,
    }
    for i, key in enumerate(section_keys):
        if key in comp_def:
            comp_def[key]["displayOrder"] = i

    font_packs = await _extract_font_packs(comp_def)

    # Save page
    from app.agents.appbuilder.tools._shared import get_saas_client
    api_client = get_saas_client()

    page_def = {
        "name": page_name, "appCode": app_code, "clientCode": client_code,
        "rootComponent": "root", "componentDefinition": comp_def,
        "eventFunctions": {}, "properties": {}, "translations": {},
        "message": f"Hybrid clone from {url}",
    }

    existing_page = None
    list_result = await api_client.get(
        "/api/ui/pages", headers=headers,
        params={"page": 0, "size": 1, "appCode": app_code, "name": page_name},
    )
    if list_result.success:
        content = list_result.data.get("content", []) if isinstance(list_result.data, dict) else []
        if content:
            existing_page = content[0]

    page_id = None
    if existing_page:
        page_id = existing_page.get("id")
        full_result = await api_client.get(f"/api/ui/pages/{page_id}", headers=headers)
        if full_result.success:
            data = full_result.data
            data["componentDefinition"] = comp_def
            data["rootComponent"] = "root"
            data["message"] = f"Hybrid clone from {url}"
            await api_client.put(f"/api/ui/pages/{page_id}", headers=headers, json=data)
    else:
        save_result = await api_client.post("/api/ui/pages", headers=headers, json=page_def)
        if save_result.success:
            page_id = save_result.data.get("id", "?") if isinstance(save_result.data, dict) else "?"

    # Update font/icon packs
    if font_packs or icon_packs:
        try:
            app_list = await api_client.get("/api/ui/applications", headers=headers,
                                           params={"page": 0, "size": 1, "appCode": app_code})
            if app_list.success:
                apps = app_list.data.get("content", []) if isinstance(app_list.data, dict) else []
                if apps:
                    app_id = apps[0].get("id")
                    app_full = await api_client.get(f"/api/ui/applications/{app_id}", headers=headers)
                    if app_full.success:
                        app_data = app_full.data
                        if font_packs:
                            existing_fp = app_data.get("properties", {}).get("fontPacks", {})
                            existing_names = {fp.get("name", "").lower() for fp in existing_fp.values()}
                            new_fp = {k: v for k, v in font_packs.items() if v["name"].lower() not in existing_names}
                            if new_fp:
                                app_data.setdefault("properties", {}).setdefault("fontPacks", {}).update(new_fp)
                        if icon_packs:
                            existing_ip = app_data.get("properties", {}).get("iconPacks", {})
                            existing_inames = {ip.get("name", "") for ip in existing_ip.values()}
                            for pn in icon_packs:
                                if pn not in existing_inames:
                                    app_data.setdefault("properties", {}).setdefault("iconPacks", {})[uuid.uuid4().hex[:22]] = {"name": pn}
                        app_data["message"] = f"Packs for {page_name}"
                        await api_client.put(f"/api/ui/applications/{app_id}", headers=headers, json=app_data)
        except Exception as e:
            logger.warning("Pack update failed: %s", e)

    # ── Step 6: QA ──
    qa_summary = ""
    try:
        from app.agents.appbuilder.tools.visual_qa import take_multi_viewport_screenshots, compute_similarity
        referer = context.get("referer", "")
        if referer:
            from urllib.parse import urlparse
            parsed = urlparse(referer)
            base_url = f"{parsed.scheme}://{parsed.netloc}"
        else:
            fh = headers.get("X-Forwarded-Host", "apps.local.modlix.com").split(",")[0].strip()
            scheme = "http" if "localhost" in fh else "https"
            base_url = f"{scheme}://{fh}"
        gen_url = f"{base_url}/{app_code}/{client_code}/page/{page_name}"
        gen_ss = await take_multi_viewport_screenshots(gen_url)
        if gen_ss and gen_ss.get("desktop"):
            score = compute_similarity(screenshot_b64, gen_ss["desktop"])
            qa_summary = f"\nSimilarity: {score}% desktop"
            logger.info("QA: %s", qa_summary.strip())
    except Exception as e:
        qa_summary = f"\nQA: skipped ({e})"

    # Clean up
    session_ctx.pop("user_screenshot_b64", None)
    session_ctx.pop("user_screenshot_mime", None)

    return ToolResult(
        success=True,
        summary=(
            f"{'Updated' if existing_page else 'Created'} page '{page_name}' "
            f"with {len(comp_def)} components ({len(section_keys)} sections). "
            f"{rewrite_count} images re-uploaded from source site."
            f"{qa_summary}\nPage ID: {page_id}"
        ),
        result_tier=ResultTier.COMPACT,
    )


HYBRID_CLONE = ToolDefinition(
    name="hybrid_clone",
    display_name="Hybrid Clone",
    description=(
        "Clone a website using a hybrid approach: screenshot-based visual layout "
        "analysis (gpt-4o vision) combined with Playwright DOM extraction for real "
        "image URLs. Produces better layout than DOM-only cloning and real images "
        "unlike screenshot-only. Use for high-fidelity website cloning."
    ),
    parameters=[
        ToolParameter(name="url", type="string", description="URL to clone.", required=True),
        ToolParameter(name="page_name", type="string", description="Page name.", required=True),
        ToolParameter(name="app_code", type="string", description="Application code.", required=False),
    ],
    execute=_execute_hybrid_clone,
    is_deferred=True,
    search_hint="hybrid clone website visual layout images DOM screenshot high fidelity",
    result_tier=ResultTier.COMPACT,
)
