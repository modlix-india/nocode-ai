"""Vision-first website converter — uses LLM vision to generate Modlix components.

Instead of programmatically converting DOM elements, this approach:
1. Screenshots the source page at multiple viewports
2. Extracts content inventory (text, images, links) with Playwright
3. Sends screenshot + content to Gemini Pro vision
4. LLM generates the componentDefinition directly in Modlix format

This produces much higher visual fidelity because the LLM "sees" the layout
and translates it to Modlix components with proper styling.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import uuid
from typing import Any

logger = logging.getLogger(__name__)


async def vision_scrape_and_convert(
    url: str,
    page_name: str = "home",
    app_code: str = "",
    client_code: str = "",
) -> dict[str, Any]:
    """Hybrid conversion — programmatic extraction for content + vision for styles.

    1. Playwright extracts ALL components with content (text, images, links)
    2. Screenshot the source page
    3. Send screenshot + component tree to Gemini to fix styles
    4. Result: complete content + visually accurate styles
    """
    # Step 1: Programmatic extraction (captures all content faithfully)
    from app.agents.appbuilder.tools.html_to_modlix import scrape_and_convert
    page_def = await scrape_and_convert(url, page_name, app_code, client_code)

    comp_def = page_def["componentDefinition"]
    logger.info("Programmatic extraction: %d components", len(comp_def))

    # Step 2: Screenshot the source for visual reference
    try:
        from playwright.async_api import async_playwright
        import base64

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            pg = await browser.new_page(viewport={"width": 1440, "height": 900})
            await pg.goto(url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(1)
            screenshot_bytes = await pg.screenshot(full_page=True)
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode("ascii")
            await browser.close()

        logger.info("Source screenshot: %d bytes", len(screenshot_bytes))

        # Step 3: Use vision to restyle the extracted components
        await _vision_restyle(comp_def, screenshot_b64, url)

    except Exception as e:
        logger.warning("Vision restyling failed: %s — using programmatic styles only", e)

    return page_def


async def _vision_convert(
    url: str,
    page_name: str,
    app_code: str,
    client_code: str,
) -> dict[str, Any]:
    """Core vision conversion pipeline."""
    from playwright.async_api import async_playwright

    logger.info("Vision conversion: screenshotting %s", url)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(1)

        # 1. Take full-page screenshot
        screenshot_bytes = await page.screenshot(full_page=True)
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode("ascii")
        logger.info("Screenshot: %d bytes", len(screenshot_bytes))

        # 2. Extract content inventory
        content = await page.evaluate(_CONTENT_EXTRACT_JS)
        logger.info("Content: %d sections, %d images, %d links",
                     len(content.get("sections", [])),
                     len(content.get("images", [])),
                     len(content.get("links", [])))

        # 3. Extract body styles for root
        body_styles = await page.evaluate("""() => {
            const cs = window.getComputedStyle(document.body);
            return {
                fontFamily: cs.getPropertyValue('font-family'),
                fontSize: cs.getPropertyValue('font-size'),
                color: cs.getPropertyValue('color'),
                backgroundColor: cs.getPropertyValue('background-color'),
            };
        }""")

        await browser.close()

    # 4. Call LLM vision to generate componentDefinition
    comp_def = await _generate_components_with_vision(
        screenshot_b64, content, body_styles, url,
    )

    if not comp_def or len(comp_def) < 3:
        raise ValueError(f"Vision LLM returned too few components: {len(comp_def) if comp_def else 0}")

    logger.info("Vision LLM generated %d components", len(comp_def))

    # 5. Extract fonts and build font packs
    from app.agents.appbuilder.tools.html_to_modlix import _extract_font_packs
    font_packs = await _extract_font_packs(comp_def)

    return {
        "name": page_name,
        "appCode": app_code,
        "clientCode": client_code,
        "rootComponent": "root",
        "componentDefinition": comp_def,
        "eventFunctions": {},
        "properties": {},
        "translations": {},
        "message": f"Vision-cloned from {url}",
        "_fontPacks": font_packs,
    }


# JS to extract content inventory from the page — text, images, links, sections.
# This is NOT for styles — just content that the LLM needs to reproduce.
_RESTYLE_PROMPT = """\
You are a CSS expert. I have a component tree extracted from a website, but the styles are wrong.
Look at the SOURCE SCREENSHOT and fix the styles on these components to match the visual design.

Return a JSON object mapping component keys to their corrected styleProperties.
Format: {"componentKey": {"resolutions": {"ALL": {"cssProp": {"value": "val"}}}}}

ONLY include components whose styles need to change. Don't include components that already look correct.

Focus on THE BIGGEST visual differences first:
1. Background colors — dark headers (#0a1229), colored sections, gradients
2. Layout direction — set {"layout": "ROWLAYOUT"} on components that should be horizontal (navbars, card grids, sidebars)
3. Width/height — sections should be width: 100%, images need proper sizing
4. Font sizes and weights — headings vs body text (DO NOT change fontFamily — it's already set correctly)
5. Colors — text colors, link colors
6. Spacing — padding, margin, gap between elements
7. Position — fixed headers, absolute overlays

DO NOT include fontFamily in your fixes — fonts are already resolved and set correctly.

RULES:
- Style values are ALWAYS strings: {"value": "16px"}, not {"value": 16}
- Use camelCase: backgroundColor, fontSize, paddingLeft
- For layout changes, return as: {"componentKey": {"properties": {"layout": {"value": "ROWLAYOUT"}}}}
- For components needing BOTH style and property changes, include both keys

COMPONENT TREE (key, type, text preview, current styles):
"""


async def _vision_restyle(
    comp_def: dict[str, Any],
    screenshot_b64: str,
    url: str,
) -> None:
    """Use LLM vision to fix styles on programmatically extracted components.

    Modifies comp_def in place.
    """
    try:
        import google.generativeai as genai
        from app.config import settings

        if not settings.GOOGLE_API_KEY:
            return

        genai.configure(api_key=settings.GOOGLE_API_KEY)
        model = genai.GenerativeModel("gemini-2.0-flash")
    except (ImportError, Exception) as e:
        logger.warning("Cannot run vision restyling: %s", e)
        return

    # Build a compact tree description
    tree_lines = []
    for key, comp in comp_def.items():
        if key == "root":
            continue
        ctype = comp.get("type", "?")
        props = comp.get("properties", {})
        text = props.get("text", {}).get("value", "")[:50]
        label = props.get("label", {}).get("value", "")[:30]
        src = props.get("src", {}).get("value", "")[:60]
        layout = props.get("layout", {}).get("value", "")
        children_count = len(comp.get("children", {}))

        # Current styles summary
        style_keys = []
        sp = comp.get("styleProperties", {})
        for sdata in sp.values():
            for k in sdata.get("resolutions", {}).get("ALL", {}):
                style_keys.append(k)

        line = f"  {key} ({ctype})"
        if text:
            line += f' "{text}"'
        if label:
            line += f' btn="{label}"'
        if src:
            line += f" src={src.split('/')[-1][:30]}"
        if layout:
            line += f" layout={layout}"
        if children_count:
            line += f" [{children_count} children]"
        if style_keys:
            line += f" styles:[{','.join(style_keys[:8])}]"
        tree_lines.append(line)

    tree_text = "\n".join(tree_lines[:80])  # Cap at 80 components

    prompt = (
        _RESTYLE_PROMPT + tree_text
        + f"\n\nSource URL: {url}"
        + "\n\nReturn ONLY the JSON object with style fixes. No explanation."
    )

    screenshot_bytes = base64.b64decode(screenshot_b64)

    logger.info("Vision restyling: %d components, %d char prompt", len(comp_def), len(prompt))

    try:
        response = await asyncio.to_thread(
            model.generate_content,
            [
                {"mime_type": "image/png", "data": screenshot_bytes},
                prompt,
            ],
            generation_config={"max_output_tokens": 32768, "temperature": 0.1},
        )

        response_text = response.text or "{}"
        if "```" in response_text:
            match = re.search(r"```(?:json)?\s*([\s\S]*?)```", response_text)
            if match:
                response_text = match.group(1)

        fixes = json.loads(response_text)
        if not isinstance(fixes, dict):
            logger.warning("Vision restyle returned non-dict: %s", type(fixes))
            return

        applied = 0
        for key, fix_data in fixes.items():
            if key not in comp_def:
                continue

            comp = comp_def[key]

            # Apply property changes (like layout: ROWLAYOUT)
            if "properties" in fix_data:
                for prop, val in fix_data["properties"].items():
                    if isinstance(val, dict):
                        comp.setdefault("properties", {})[prop] = val
                    else:
                        comp.setdefault("properties", {})[prop] = {"value": str(val)}

            # Apply style changes
            resolutions = fix_data.get("resolutions")
            if not resolutions and "properties" not in fix_data:
                # Might be flat style dict — wrap it
                if any(k in fix_data for k in ("backgroundColor", "color", "fontSize", "width", "height", "padding")):
                    resolutions = {"ALL": {k: {"value": str(v)} if not isinstance(v, dict) else v for k, v in fix_data.items()}}

            if resolutions:
                # Remove fontFamily — already resolved by font replacement pipeline
                for res_data in resolutions.values():
                    res_data.pop("fontFamily", None)

                sp = comp.get("styleProperties", {})
                if sp:
                    # Merge into first style entry
                    first_sid = next(iter(sp))
                    for res_name, res_data in resolutions.items():
                        if not res_data:
                            continue
                        existing_res = sp[first_sid].setdefault("resolutions", {}).setdefault(res_name, {})
                        for prop, val in res_data.items():
                            if isinstance(val, dict):
                                existing_res[prop] = val
                            else:
                                existing_res[prop] = {"value": str(val)}
                else:
                    style_id = uuid.uuid4().hex[:22]
                    # Fix values format
                    for res_name, res_data in resolutions.items():
                        for prop, val in list(res_data.items()):
                            if not isinstance(val, dict):
                                res_data[prop] = {"value": str(val)}
                    comp["styleProperties"] = {style_id: {"resolutions": resolutions}}

                applied += 1

        logger.info("Vision restyling: applied style fixes to %d/%d components", applied, len(fixes))

    except Exception as e:
        logger.warning("Vision restyling failed: %s", e)


_CONTENT_EXTRACT_JS = """() => {
    const sections = [];
    const images = [];
    const links = [];
    const iframes = [];

    function walk(el, depth) {
        if (depth > 10) return;
        const tag = (el.tagName || '').toLowerCase();
        if (['script','style','noscript','link','meta','head'].includes(tag)) return;
        const rect = el.getBoundingClientRect();
        if (rect.width < 5 || rect.height < 5) return;
        if (rect.top > 8000) return;

        // Collect images
        if (tag === 'img') {
            const src = el.src || el.dataset?.src || '';
            if (src) images.push({src, alt: el.alt || '', w: Math.round(rect.width), h: Math.round(rect.height), y: Math.round(rect.y)});
        }
        // Collect iframes
        if (tag === 'iframe') {
            iframes.push({src: el.src || '', w: Math.round(rect.width), h: Math.round(rect.height), y: Math.round(rect.y)});
        }
        // Collect links
        if (tag === 'a' && el.href) {
            const text = el.textContent?.trim().substring(0, 100) || '';
            if (text) links.push({href: el.href, text, y: Math.round(rect.y)});
        }
        // Collect major sections
        if (['header','footer','nav','main','article','section','aside'].includes(tag) ||
            (tag === 'div' && depth <= 2 && el.children.length > 0 && rect.height > 50)) {
            const computed = window.getComputedStyle(el);
            const bg = computed.getPropertyValue('background-color');
            const bgImg = computed.getPropertyValue('background-image');
            const text = [];
            for (const node of el.childNodes) {
                if (node.nodeType === 3) {
                    const t = node.textContent?.trim();
                    if (t) text.push(t.substring(0, 200));
                }
            }
            sections.push({
                tag, id: el.id || '',
                y: Math.round(rect.y), h: Math.round(rect.height), w: Math.round(rect.width),
                bg: bg !== 'rgba(0, 0, 0, 0)' ? bg : '',
                bgImg: bgImg !== 'none' ? bgImg.substring(0, 200) : '',
                childCount: el.children.length,
                text: text.join(' ').substring(0, 300),
            });
        }

        for (const child of el.children) walk(child, depth + 1);
    }
    walk(document.body, 0);

    // Collect all visible text blocks (for content accuracy)
    const textBlocks = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
    let node;
    while (node = walker.nextNode()) {
        const t = node.textContent?.trim();
        if (!t || t.length < 3) continue;
        const range = document.createRange();
        range.selectNodeContents(node);
        const r = range.getBoundingClientRect();
        if (r.width < 5 || r.height < 5) continue;
        if (r.top > 5000) continue;
        textBlocks.push({text: t.substring(0, 500), y: Math.round(r.y), h: Math.round(r.height)});
    }

    return {sections, images, links, iframes, textBlocks};
}"""


_VISION_PROMPT = """\
You are a pixel-perfect web-to-Modlix converter. Given a screenshot of a website, generate the Modlix componentDefinition JSON that reproduces this page as closely as possible.

## Modlix Component Format

Each component is an entry in a flat `componentDefinition` dict keyed by a unique ID.

### Component Types:
- **Grid**: Flex container (default: column direction). Set `properties.layout.value = "ROWLAYOUT"` for horizontal.
- **Text**: Text content. Set `properties.text.value = "content"`.
- **Image**: Image. Set `properties.src.value = "url"` and `properties.alt.value = "desc"`.
- **Button**: Button. Set `properties.label.value = "text"`.
- **Iframe**: Embedded content. Set `properties.src.value = "url"`.

### Structure:
```json
{
  "componentKey": {
    "key": "componentKey",
    "name": "descriptive name",
    "type": "Grid|Text|Image|Button|Iframe",
    "properties": {
      "propName": {"value": "propValue"}
    },
    "styleProperties": {
      "styleId": {
        "resolutions": {
          "ALL": {
            "cssProperty": {"value": "cssValue"},
            "anotherProp": {"value": "value"}
          },
          "MOBILE_LANDSCAPE_SCREEN_SMALL": {
            "cssProperty": {"value": "mobileValue"}
          }
        }
      }
    },
    "children": {"childKey1": true, "childKey2": true},
    "displayOrder": 0
  }
}
```

### Critical Rules:
1. **Root component** must have key "root", type "Grid", with `width: 100vw`, `height: 100vh`, `overflow: auto`, `gap: 0`.
2. **Grid is COLUMN by default**. For horizontal layouts, set `properties.layout.value = "ROWLAYOUT"`.
3. **Grid default gap is 5px** in Modlix. Always set `gap: 0` unless you want spacing.
4. **All style values are strings**: `{"value": "16px"}`, `{"value": "#0a1229"}`, `{"value": "bold"}`.
5. **Use camelCase** for CSS properties: `backgroundColor`, `fontSize`, `paddingLeft`, etc.
6. **displayOrder** controls rendering order within parent. Start from 0.
7. **children** is a dict of `{childKey: true}`, not an array.
8. **Generate unique keys** — use descriptive names like "navBar", "heroSection", "footerGrid".
9. **Include MOBILE_LANDSCAPE_SCREEN_SMALL** overrides for responsive elements (font sizes, padding, layout changes).
10. **For hover styles**, use `"cssProperty:hover": {"value": "val"}` inline in the ALL resolution.
11. **External image URLs** must be absolute (use the actual URLs from the content inventory below).

### Style Properties to Use:
Layout: `justifyContent`, `alignItems`, `gap`, `flexWrap`
Sizing: `width`, `height`, `minHeight`, `maxWidth`, `padding*`, `margin*`
Background: `backgroundColor`, `backgroundImage`, `backgroundSize`, `backgroundPosition`
Typography: `color`, `fontSize`, `fontFamily`, `fontWeight`, `lineHeight`, `letterSpacing`
Border: `border*Width`, `border*Color`, `border*Radius`
Position: `position`, `top`, `left`, `right`, `bottom`, `zIndex`
Visual: `opacity`, `boxShadow`, `overflow`, `cursor`, `textDecoration`

## Example — Navigation Bar:
```json
{
  "navBar": {
    "key": "navBar", "name": "Navigation Bar", "type": "Grid",
    "properties": {"layout": {"value": "ROWLAYOUT"}},
    "styleProperties": {
      "s1": {"resolutions": {"ALL": {
        "height": {"value": "70px"}, "backgroundColor": {"value": "#0a1229"},
        "justifyContent": {"value": "space-between"}, "alignItems": {"value": "center"},
        "paddingLeft": {"value": "40px"}, "paddingRight": {"value": "40px"},
        "width": {"value": "100%"}, "gap": {"value": "0"}
      }}}
    },
    "children": {"logo": true, "navLinks": true, "ctaButton": true},
    "displayOrder": 0
  },
  "logo": {
    "key": "logo", "name": "Logo", "type": "Image",
    "properties": {"src": {"value": "https://example.com/logo.svg"}, "alt": {"value": "Logo"}},
    "styleProperties": {
      "s2": {"resolutions": {"ALL": {"height": {"value": "30px"}, "width": {"value": "auto"}}}}
    },
    "children": {}, "displayOrder": 0
  }
}
```

## Your Task

Look at the screenshot and generate a COMPLETE componentDefinition that reproduces the visual layout.
Focus on:
- Correct section structure (header, hero, content areas, footer)
- Background colors and images (USE EXACT colors from the screenshot — dark headers are often #0a1229 or similar dark blues)
- Typography (sizes, weights, colors)
- Horizontal vs vertical layouts (navbars are ALWAYS ROWLAYOUT with space-between)
- Spacing and alignment
- Images and their sizing (use the EXACT image URLs from the content inventory)
- Responsive mobile overrides for key elements

IMPORTANT: Use the EXACT font family from the "Body font" field below for all text. Set it on the root component's fontFamily so all children inherit it. Use ONLY the primary font name (e.g. "Montserrat" not "Montserrat, sans-serif").

## Content Inventory (extracted from the page):
"""


async def _generate_components_with_vision(
    screenshot_b64: str,
    content: dict[str, Any],
    body_styles: dict[str, str],
    url: str,
) -> dict[str, Any]:
    """Use Gemini Pro vision to generate componentDefinition from screenshot."""
    try:
        import google.generativeai as genai
        from app.config import settings

        if not settings.GOOGLE_API_KEY:
            raise ValueError("No GOOGLE_API_KEY configured")

        genai.configure(api_key=settings.GOOGLE_API_KEY)
        # Use Gemini Pro for better quality (flash is too superficial for this)
        model = genai.GenerativeModel("gemini-2.0-flash")
    except ImportError:
        raise ValueError("google-generativeai not installed")

    # Build content summary for the prompt
    content_summary = _build_content_summary(content, body_styles, url)

    prompt = (
        _VISION_PROMPT
        + content_summary
        + "\n\nGenerate the COMPLETE componentDefinition JSON object. "
        + "Return ONLY valid JSON — no explanation, no markdown fencing."
    )

    screenshot_bytes = base64.b64decode(screenshot_b64)

    logger.info("Calling Gemini vision with %d byte screenshot and %d char prompt",
                len(screenshot_bytes), len(prompt))

    response = await asyncio.to_thread(
        model.generate_content,
        [
            {"mime_type": "image/png", "data": screenshot_bytes},
            prompt,
        ],
        generation_config={"max_output_tokens": 65536, "temperature": 0.1},
    )

    response_text = response.text or "{}"

    # Extract JSON from response
    if "```" in response_text:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", response_text)
        if match:
            response_text = match.group(1)

    # Try to parse
    try:
        comp_def = json.loads(response_text)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse LLM response as JSON: %s", e)
        logger.error("Response (first 500 chars): %s", response_text[:500])
        raise ValueError(f"LLM returned invalid JSON: {e}")

    if not isinstance(comp_def, dict):
        raise ValueError(f"LLM returned {type(comp_def)}, expected dict")

    # Validate and fix the component definition
    comp_def = _validate_and_fix(comp_def)

    return comp_def


def _build_content_summary(
    content: dict[str, Any],
    body_styles: dict[str, str],
    url: str,
) -> str:
    """Build a compact content summary for the LLM prompt."""
    # Extract primary font name from the font-family string
    raw_font = body_styles.get("fontFamily", "sans-serif")
    primary_font = raw_font.split(",")[0].strip().strip("'\"") if raw_font else "sans-serif"

    lines = [f"Source URL: {url}"]
    lines.append(f"Body font: {raw_font}")
    lines.append(f"PRIMARY FONT TO USE: {primary_font}")
    lines.append(f"Body color: {body_styles.get('color', '#000')}")
    lines.append(f"Body background: {body_styles.get('backgroundColor', '#fff')}")
    lines.append("")

    # Sections
    sections = content.get("sections", [])
    if sections:
        lines.append(f"Page sections ({len(sections)}):")
        for s in sections[:15]:
            line = f"  <{s['tag']}{'#'+s['id'] if s['id'] else ''}> y={s['y']} h={s['h']} children={s['childCount']}"
            if s.get("bg"):
                line += f" bg={s['bg']}"
            if s.get("bgImg"):
                line += f" bgImg={s['bgImg'][:80]}"
            lines.append(line)
        lines.append("")

    # Images
    images = content.get("images", [])
    if images:
        lines.append(f"Images ({len(images)}):")
        for img in images[:10]:
            lines.append(f"  src={img['src'][:120]} alt={img.get('alt', '')!r} {img['w']}x{img['h']} y={img['y']}")
        lines.append("")

    # Iframes
    iframes = content.get("iframes", [])
    if iframes:
        lines.append(f"Iframes ({len(iframes)}):")
        for iframe in iframes[:5]:
            lines.append(f"  src={iframe['src'][:120]} {iframe['w']}x{iframe['h']} y={iframe['y']}")
        lines.append("")

    # Key text blocks (sorted by y position, deduplicated)
    text_blocks = content.get("textBlocks", [])
    if text_blocks:
        text_blocks.sort(key=lambda t: t["y"])
        lines.append(f"Text content (top-to-bottom, {len(text_blocks)} blocks):")
        seen = set()
        count = 0
        for tb in text_blocks:
            text = tb["text"][:120]
            if text in seen or len(text) < 5:
                continue
            seen.add(text)
            lines.append(f"  y={tb['y']}: {text!r}")
            count += 1
            if count >= 30:
                break
        lines.append("")

    # Links
    links = content.get("links", [])
    if links:
        lines.append(f"Navigation links ({len(links)}):")
        seen_links = set()
        for link in links[:15]:
            if link["text"] in seen_links:
                continue
            seen_links.add(link["text"])
            lines.append(f"  \"{link['text']}\" → {link['href'][:100]}")
        lines.append("")

    return "\n".join(lines)


def _validate_and_fix(comp_def: dict[str, Any]) -> dict[str, Any]:
    """Validate and fix common LLM mistakes in the generated componentDefinition."""

    # Ensure root exists
    if "root" not in comp_def:
        # Try to find a root-like component
        for key, comp in comp_def.items():
            if comp.get("name", "").lower() in ("root", "rootgrid", "page"):
                comp_def["root"] = comp
                comp_def["root"]["key"] = "root"
                if key != "root":
                    del comp_def[key]
                    # Update any children references
                    for c in comp_def.values():
                        children = c.get("children", {})
                        if key in children:
                            del children[key]
                            children["root"] = True
                break
        else:
            # Create a root that wraps all top-level components
            top_level = set(comp_def.keys())
            for comp in comp_def.values():
                for child_key in comp.get("children", {}):
                    top_level.discard(child_key)

            root_children = {k: True for k in sorted(top_level)}
            comp_def["root"] = {
                "key": "root", "name": "rootGrid", "type": "Grid",
                "properties": {},
                "styleProperties": {
                    uuid.uuid4().hex[:22]: {
                        "resolutions": {
                            "ALL": {
                                "width": {"value": "100vw"},
                                "height": {"value": "100vh"},
                                "overflow": {"value": "auto"},
                                "gap": {"value": "0"},
                            }
                        }
                    }
                },
                "children": root_children,
                "displayOrder": 0,
            }

    # Fix each component
    for key, comp in list(comp_def.items()):
        # Ensure required fields
        if "key" not in comp:
            comp["key"] = key
        if "type" not in comp:
            comp["type"] = "Grid"
        if "children" not in comp:
            comp["children"] = {}
        if "displayOrder" not in comp:
            comp["displayOrder"] = 0
        if "styleProperties" not in comp:
            comp["styleProperties"] = {}
        if "properties" not in comp:
            comp["properties"] = {}

        # Fix styleProperties format — LLM sometimes uses wrong nesting
        sp = comp.get("styleProperties", {})
        if sp:
            for sid, sdata in list(sp.items()):
                if not isinstance(sdata, dict):
                    del sp[sid]
                    continue
                # If LLM put CSS directly in the style entry (no resolutions wrapper)
                if "resolutions" not in sdata:
                    # Check if it looks like CSS properties
                    if any(k in sdata for k in ("backgroundColor", "fontSize", "width", "color", "height")):
                        sp[sid] = {"resolutions": {"ALL": {
                            k: {"value": str(v)} if not isinstance(v, dict) else v
                            for k, v in sdata.items()
                        }}}
                else:
                    # Fix style values that aren't wrapped in {"value": ...}
                    for res_name, res_data in sdata.get("resolutions", {}).items():
                        if isinstance(res_data, dict):
                            for prop, val in list(res_data.items()):
                                if not isinstance(val, dict):
                                    res_data[prop] = {"value": str(val)}

        # Fix property values
        for prop, val in list(comp.get("properties", {}).items()):
            if not isinstance(val, dict):
                comp["properties"][prop] = {"value": str(val)}

        # Fix children format — LLM sometimes uses arrays
        children = comp.get("children", {})
        if isinstance(children, list):
            comp["children"] = {str(c): True for c in children}
        elif isinstance(children, dict):
            for ck, cv in list(children.items()):
                if cv is not True:
                    children[ck] = True

        # Ensure Grid default gap
        if comp.get("type") == "Grid" and key != "root":
            has_gap = False
            for sdata in sp.values():
                if "gap" in sdata.get("resolutions", {}).get("ALL", {}):
                    has_gap = True
                    break
            if not has_gap:
                # Add gap: 0 to first style entry or create one
                if sp:
                    first_sid = next(iter(sp))
                    sp[first_sid].setdefault("resolutions", {}).setdefault("ALL", {})["gap"] = {"value": "0"}
                else:
                    comp["styleProperties"] = {
                        uuid.uuid4().hex[:22]: {
                            "resolutions": {"ALL": {"gap": {"value": "0"}}}
                        }
                    }

    # Verify all children references are valid
    for comp in comp_def.values():
        for child_key in list(comp.get("children", {}).keys()):
            if child_key not in comp_def:
                del comp["children"][child_key]
                logger.warning("Removed dangling child reference: %s", child_key)

    return comp_def


_REFINE_PROMPT = """\
You are a pixel-perfect web page refinement expert. You are given THREE inputs:
1. IMAGE 1: The SOURCE website (target design to match)
2. IMAGE 2: The GENERATED page (current state that needs improvement)
3. The current componentDefinition JSON of the generated page

Compare them carefully. The generated page has issues — some visual elements are wrong, missing, or misaligned compared to the source.

Generate a COMPLETE REPLACEMENT componentDefinition JSON that fixes ALL the differences you see.

Key differences to look for and fix:
- Missing dark/colored backgrounds on header, footer, or sections
- Wrong layout direction (items stacked when they should be side-by-side, or vice versa)
- Missing images or wrong image sizing
- Wrong colors (text, backgrounds)
- Missing or wrong spacing/padding
- Wrong font sizes or weights
- Missing sections entirely
- Wrong content order

IMPORTANT RULES:
- Return the COMPLETE componentDefinition — not just the changes.
- Root component must have key "root" with width: 100vw, height: 100vh, overflow: auto, gap: 0
- Grid default direction is COLUMN. Use properties.layout.value = "ROWLAYOUT" for horizontal.
- All Grid components must have gap: 0 unless specific spacing is needed.
- All style values are strings: {"value": "16px"}, {"value": "#0a1229"}
- Use camelCase for CSS properties.
- Keep the same component keys where possible so content is preserved.
- Include MOBILE_LANDSCAPE_SCREEN_SMALL overrides for responsive elements.

Return ONLY valid JSON. No explanation, no markdown fencing.
"""


async def iterative_vision_refine(
    source_url: str,
    generated_page_url: str,
    comp_def: dict[str, Any],
    max_iterations: int = 2,
    save_callback=None,
) -> int:
    """Iteratively refine the generated page by comparing screenshots.

    Takes screenshots of source and generated page, sends both to the LLM
    along with the current componentDefinition, and asks for a corrected version.

    Returns number of successful improvement iterations.
    """
    from app.agents.appbuilder.tools.visual_qa import (
        take_multi_viewport_screenshots, compute_similarity, VIEWPORTS,
    )
    import copy

    try:
        import google.generativeai as genai
        from app.config import settings
        if not settings.GOOGLE_API_KEY:
            return 0
        genai.configure(api_key=settings.GOOGLE_API_KEY)
        model = genai.GenerativeModel("gemini-2.0-flash")
    except (ImportError, Exception) as e:
        logger.warning("Cannot run vision refinement: %s", e)
        return 0

    # Screenshot source once
    source_screenshots = await take_multi_viewport_screenshots(source_url)
    if not source_screenshots.get("desktop"):
        logger.warning("Cannot screenshot source — skipping refinement")
        return 0

    improvements = 0
    prev_similarity = 0.0

    for iteration in range(max_iterations):
        logger.info("Vision refinement iteration %d/%d", iteration + 1, max_iterations)

        gen_screenshots = await take_multi_viewport_screenshots(generated_page_url)
        if not gen_screenshots.get("desktop"):
            break

        # Compute similarity
        scores = {}
        for vp in VIEWPORTS:
            src = source_screenshots.get(vp)
            gen = gen_screenshots.get(vp)
            if src and gen:
                scores[vp] = compute_similarity(src, gen)
        avg = sum(scores.values()) / max(len(scores), 1)
        logger.info("Refinement iteration %d: similarity=%.1f%% %s",
                     iteration + 1, avg, {k: f"{v:.1f}%" for k, v in scores.items()})

        if avg >= 85:
            logger.info("Similarity %.1f%% >= 85%% — refinement complete", avg)
            break
        if iteration > 0 and avg - prev_similarity < 2.0:
            logger.info("Similarity plateaued at %.1f%% — stopping", avg)
            break
        prev_similarity = avg

        # Build compact JSON of current comp_def
        comp_json = json.dumps(comp_def, indent=1)
        if len(comp_json) > 30000:
            comp_json = comp_json[:30000] + "\n... (truncated)"

        prompt = (
            _REFINE_PROMPT
            + f"\n\nCurrent componentDefinition ({len(comp_def)} components):\n"
            + comp_json
        )

        source_bytes = base64.b64decode(source_screenshots["desktop"])
        gen_bytes = base64.b64decode(gen_screenshots["desktop"])

        try:
            response = await asyncio.to_thread(
                model.generate_content,
                [
                    {"mime_type": "image/png", "data": source_bytes},
                    {"mime_type": "image/png", "data": gen_bytes},
                    prompt,
                ],
                generation_config={"max_output_tokens": 65536, "temperature": 0.1},
            )

            response_text = response.text or "{}"
            if "```" in response_text:
                match = re.search(r"```(?:json)?\s*([\s\S]*?)```", response_text)
                if match:
                    response_text = match.group(1)

            new_comp_def = json.loads(response_text)
            if not isinstance(new_comp_def, dict) or len(new_comp_def) < 3:
                logger.warning("Refinement returned too few components")
                break

            new_comp_def = _validate_and_fix(new_comp_def)
            logger.info("Refinement generated %d components (was %d)",
                        len(new_comp_def), len(comp_def))

            # Snapshot, apply, save, verify
            snapshot = copy.deepcopy(comp_def)
            comp_def.clear()
            comp_def.update(new_comp_def)

            if save_callback:
                saved = await save_callback(comp_def)
                if not saved:
                    comp_def.clear()
                    comp_def.update(snapshot)
                    break

                verify_screenshots = await take_multi_viewport_screenshots(generated_page_url)
                if verify_screenshots:
                    new_scores = {}
                    for vp in VIEWPORTS:
                        src = source_screenshots.get(vp)
                        ver = verify_screenshots.get(vp)
                        if src and ver:
                            new_scores[vp] = compute_similarity(src, ver)
                    new_avg = sum(new_scores.values()) / max(len(new_scores), 1)

                    if new_avg < avg - 1.0:
                        logger.warning("Refinement DROPPED %.1f%% → %.1f%% — rolling back",
                                        avg, new_avg)
                        comp_def.clear()
                        comp_def.update(snapshot)
                        await save_callback(snapshot)
                        break
                    else:
                        logger.info("Refinement improved: %.1f%% → %.1f%%", avg, new_avg)
                        improvements += 1

        except Exception as e:
            logger.warning("Refinement iteration %d failed: %s", iteration + 1, e)
            break

    return improvements
