"""Build a Modlix page from a user-pasted screenshot.

Pipeline:
  Phase 0: Page-level vision scan (font, colours, brand)
  Phase 1: Slice screenshot into sections
  Phase 2: Per-slice gpt-4o → component JSON + asset manifest
  Phase 3: Resolve assets (icons, logos, images)
  Phase 4: Assemble under root + register font/icon packs
  Phase 5: Save page via API
  Phase 6: QA — screenshot generated page, compute similarity
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
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


# ── Vision LLM helper ──────────────────────────────────────────────

async def _vision_to_json(
    image_b64: str,
    prompt: str,
    max_tokens: int = 16384,
) -> dict[str, Any]:
    """Send an image + prompt to gpt-4o and parse JSON response."""
    from openai import OpenAI
    from app.config import settings

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    response = await asyncio.to_thread(
        client.chat.completions.create,
        model=settings.OPENAI_MODEL_BALANCED or "gpt-4o",
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
        messages=[
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                {"type": "text", "text": prompt},
            ]},
        ],
    )

    text = response.choices[0].message.content or "{}"
    # Strip markdown fences if present
    if "```" in text:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if m:
            text = m.group(1)
    return json.loads(text)


# ── Phase 0: Page-level scan ───────────────────────────────────────

_PHASE0_PROMPT = """\
Analyze this full-page screenshot and extract page-wide design attributes.
Return JSON only:
{
  "body_font": "font family name (e.g. Inter, Montserrat)",
  "body_color": "#hex text color",
  "body_bg": "#hex background color",
  "primary_brand": "brand name if recognizable, else null",
  "dominant_accent": "#hex accent/CTA color"
}
"""


async def _phase0_scan(thumbnail_b64: str) -> dict[str, Any]:
    """Extract page-level attributes from a thumbnail of the full screenshot."""
    logger.info("Phase 0: Page-level scan...")
    result = await _vision_to_json(thumbnail_b64, _PHASE0_PROMPT, max_tokens=512)
    logger.info("Phase 0 result: font=%s, color=%s, bg=%s, brand=%s",
                result.get("body_font"), result.get("body_color"),
                result.get("body_bg"), result.get("primary_brand"))
    return result


# ── Phase 2: Per-slice vision build ────────────────────────────────

_SLICE_PROMPT_TEMPLATE = """\
You are building ONE section of a Modlix no-code page from a screenshot slice.
Generate the componentDefinition JSON for this section ONLY.

## Modlix Component Format

Each component is a JSON object with these fields:
- key: unique string identifier (use descriptive names like "hero_title", "nav_logo")
- name: same as key
- type: one of Grid, Text, Image, Button, Icon, Video, Iframe
- properties: {{
    "containerType": {{"value": "_bare"}}  (for Grid only)
    "layout": {{"value": "ROWLAYOUT"}}     (for Grid with horizontal flex layout)
    "text": {{"value": "visible text"}}     (for Text)
    "src": {{"value": "image_url"}}         (for Image)
    "label": {{"value": "button text"}}     (for Button)
    "icon": {{"value": "fa fa-solid fa-heart"}}  (for Icon — use FontAwesome or Material Icons class names)
    "designType": {{"value": "_bare"}}      (for Text, Button, Image, Icon, Iframe, Video)
  }}
- styleProperties: {{
    "<random_id>": {{
      "resolutions": {{
        "ALL": {{
          "width": {{"value": "100%"}},
          "height": {{"value": "auto"}},
          "backgroundColor": {{"value": "#hex"}},
          "color": {{"value": "#hex"}},
          "fontSize": {{"value": "16px"}},
          "fontWeight": {{"value": "700"}},
          "padding": {{"value": "20px"}},
          "display": {{"value": "flex"}},
          "flexDirection": {{"value": "row"}},
          "justifyContent": {{"value": "center"}},
          "alignItems": {{"value": "center"}},
          "gap": {{"value": "16px"}},
          "position": {{"value": "relative"}},
          ... any CSS property in camelCase
        }}
      }}
    }}
  }}
- children: {{"child_key": true, ...}}  (for Grid containers)
- displayOrder: integer (0-based, determines render order among siblings)

## Rules
- Grid is the container type. Default layout is COLUMN. Use layout=ROWLAYOUT for horizontal flex rows.
- Every Grid must have containerType: "_bare" in properties.
- Every non-Grid component must have designType: "_bare" in properties.
- All CSS values go in styleProperties.resolutions.ALL as {{"value": "..."}} objects.
- Use exact hex colors from what you see. Be specific with pixel sizes.
- For background images, use backgroundImage: {{"value": "url('...')"}} — but since we can't extract real URLs from a screenshot, use ASSET_N placeholders for images.
- Text content: copy the EXACT visible text from the screenshot.

## CRITICAL — be thorough and detailed
- Generate a component for EVERY visible element: every heading, paragraph, button, image, icon, link, badge, card.
- Each section should have 8-25 components depending on complexity.
- Use precise CSS: exact fontSize (e.g. "48px" not "large"), exact padding (e.g. "60px 40px"), exact gap values.
- EVERY section root must have width: "100%" in styles.
- For cards/grids with multiple items side by side, use a parent Grid with layout=ROWLAYOUT, gap, and child Grids for each card.
- For hero sections with centered text over a background, use the section Grid as the bg container with position:relative, and child Text/Button components.
- Preserve the visual hierarchy: large headings (40-72px), subheadings (24-32px), body text (14-18px), small labels (12-14px).

## Icons
Use Modlix Icon component with FontAwesome or Material Icons class strings:
- FontAwesome: "fa fa-solid fa-heart", "fa fa-brands fa-instagram"
- Material Icons: "mi material-icons home", "mi material-icons search"

## Image Assets
For every visible image, logo, or photo in this slice:
- Create an Image component with src: {{"value": "ASSET_N"}} (sequential placeholder)
- Add an entry to the "assets" array in your response

## Example — a hero section with image, title, subtitle, CTA button
```json
{{
  "s0_sectionRoot": {{
    "key": "s0_sectionRoot", "name": "s0_sectionRoot", "type": "Grid",
    "properties": {{"containerType": {{"value": "_bare"}}}},
    "styleProperties": {{"st1": {{"resolutions": {{"ALL": {{
      "width": {{"value": "100%"}}, "minHeight": {{"value": "600px"}},
      "backgroundColor": {{"value": "#0a0a0a"}},
      "display": {{"value": "flex"}}, "flexDirection": {{"value": "column"}},
      "alignItems": {{"value": "center"}}, "justifyContent": {{"value": "center"}},
      "padding": {{"value": "80px 40px"}}, "gap": {{"value": "24px"}}
    }}}}}}}},
    "children": {{"s0_heroImg": true, "s0_title": true, "s0_subtitle": true, "s0_cta": true}},
    "displayOrder": 0
  }},
  "s0_heroImg": {{
    "key": "s0_heroImg", "name": "s0_heroImg", "type": "Image",
    "properties": {{"src": {{"value": "ASSET_1"}}, "alt": {{"value": "product hero"}}, "designType": {{"value": "_bare"}}}},
    "styleProperties": {{"st2": {{"resolutions": {{"ALL": {{
      "width": {{"value": "400px"}}, "height": {{"value": "400px"}}, "objectFit": {{"value": "contain"}}
    }}}}}}}},
    "children": {{}}, "displayOrder": 0
  }},
  "s0_title": {{
    "key": "s0_title", "name": "s0_title", "type": "Text",
    "properties": {{"text": {{"value": "Ring AIR"}}, "designType": {{"value": "_bare"}}}},
    "styleProperties": {{"st3": {{"resolutions": {{"ALL": {{
      "fontSize": {{"value": "56px"}}, "fontWeight": {{"value": "700"}},
      "color": {{"value": "#ffffff"}}, "textAlign": {{"value": "center"}}
    }}}}}}}},
    "children": {{}}, "displayOrder": 1
  }},
  "s0_subtitle": {{
    "key": "s0_subtitle", "name": "s0_subtitle", "type": "Text",
    "properties": {{"text": {{"value": "The most advanced health tracker"}}, "designType": {{"value": "_bare"}}}},
    "styleProperties": {{"st4": {{"resolutions": {{"ALL": {{
      "fontSize": {{"value": "18px"}}, "color": {{"value": "#cccccc"}}, "textAlign": {{"value": "center"}}
    }}}}}}}},
    "children": {{}}, "displayOrder": 2
  }},
  "s0_cta": {{
    "key": "s0_cta", "name": "s0_cta", "type": "Button",
    "properties": {{"label": {{"value": "Buy Now"}}, "designType": {{"value": "_bare"}}}},
    "styleProperties": {{"st5": {{"resolutions": {{"ALL": {{
      "backgroundColor": {{"value": "#ffffff"}}, "color": {{"value": "#000000"}},
      "padding": {{"value": "14px 32px"}}, "borderRadius": {{"value": "30px"}},
      "fontSize": {{"value": "16px"}}, "fontWeight": {{"value": "600"}}
    }}}}}}}},
    "children": {{}}, "displayOrder": 3
  }}
}}
```
This example has 5 components. A real section should have 8-20+ depending on visible elements.

## Context
- This is slice {slice_index} of {total_slices}.
- Slice dimensions: {width}x{height}px.
- Slice background: {avg_bg_color}.
- Page body font: {body_font}. Page text color: {body_color}.
- Use the body font unless this section clearly uses a different font.
- Generate AT LEAST 8 components for this section. Every visible element matters.

## Response Format
Return ONLY valid JSON:
{{
  "components": {{
    "s{slice_index}_sectionRoot": {{...}},
    "s{slice_index}_childA": {{...}},
    ...
  }},
  "assets": [
    {{"placeholder": "ASSET_1", "kind": "image|logo|text-over-image", "bbox": [x,y,w,h], "label": "description", "dominant_color": "#hex", "brand_hint": "brand_name_or_null"}}
  ],
  "fonts": [
    {{"family": "Playfair Display", "components": ["s{slice_index}_heading"]}}
  ]
}}

The TOP-LEVEL component key MUST be "s{slice_index}_sectionRoot".
All other keys must be prefixed with "s{slice_index}_".
"""


async def _phase2_build_slice(
    slice_b64: str,
    slice_index: int,
    total_slices: int,
    width: int,
    height: int,
    avg_bg_color: str,
    page_info: dict[str, Any],
) -> dict[str, Any]:
    """Run gpt-4o on one slice to produce component JSON."""
    prompt = _SLICE_PROMPT_TEMPLATE.format(
        slice_index=slice_index,
        total_slices=total_slices,
        width=width,
        height=height,
        avg_bg_color=avg_bg_color,
        body_font=page_info.get("body_font", "sans-serif"),
        body_color=page_info.get("body_color", "#000000"),
    )
    result = await _vision_to_json(slice_b64, prompt, max_tokens=16384)
    components = result.get("components", {})
    assets = result.get("assets", [])
    fonts = result.get("fonts", [])

    # Tag assets with slice index for bbox translation
    for a in assets:
        a["slice_index"] = slice_index

    logger.info("Phase 2 slice %d: %d components, %d assets, %d fonts",
                slice_index, len(components), len(assets), len(fonts))
    return {"components": components, "assets": assets, "fonts": fonts}


# ── Validation ─────────────────────────────────────────────────────

def _validate_components(comp_def: dict[str, Any]) -> dict[str, Any]:
    """Fix common issues in LLM-generated componentDefinition."""
    # Remove orphaned children references
    for key, comp in list(comp_def.items()):
        children = comp.get("children", {})
        if isinstance(children, dict):
            comp["children"] = {k: v for k, v in children.items() if k in comp_def}

    # Ensure every component has required fields
    for key, comp in comp_def.items():
        comp.setdefault("key", key)
        comp.setdefault("name", key)
        comp.setdefault("type", "Grid")
        comp.setdefault("properties", {})
        comp.setdefault("styleProperties", {})
        comp.setdefault("children", {})
        comp.setdefault("displayOrder", 0)

        # Ensure style IDs exist
        if comp["styleProperties"]:
            for sid, st in comp["styleProperties"].items():
                if "resolutions" not in st:
                    comp["styleProperties"][sid] = {"resolutions": {"ALL": st}}

    return comp_def


# ── Main tool execute ──────────────────────────────────────────────

async def _execute_build_from_screenshot(
    params: dict[str, Any],
    context: dict[str, Any],
) -> ToolResult:
    """Build a Modlix page from a screenshot attached to the conversation."""
    page_name = params.get("page_name", "screenshot_page").lower()
    app_code = params.get("app_code") or context.get("app_code", "")
    replace_existing = params.get("replace_existing", True)

    if not app_code:
        return ToolResult(success=False, error="app_code is required.")

    # Get screenshot from session context
    session_ctx = context.get("session_context", {})
    screenshot_b64 = session_ctx.get("user_screenshot_b64")
    if not screenshot_b64:
        return ToolResult(
            success=False,
            error="No screenshot attached. Paste an image in the chat and retry.",
        )

    client_code = context.get("client_code", "SYSTEM")
    headers = context["headers"]

    try:
        # Decode full screenshot
        img_bytes = base64.b64decode(screenshot_b64)
        full_img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        logger.info("Screenshot loaded: %dx%d", full_img.width, full_img.height)
    except Exception as e:
        return ToolResult(success=False, error=f"Failed to decode screenshot: {e}")

    # ── Phase 0: Page-level scan ──
    # Create a thumbnail for the overview scan
    thumb = full_img.copy()
    max_dim = 1568
    if max(thumb.size) > max_dim:
        ratio = max_dim / max(thumb.size)
        thumb = thumb.resize((int(thumb.width * ratio), int(thumb.height * ratio)), Image.LANCZOS)
    thumb_buf = io.BytesIO()
    thumb.save(thumb_buf, format="JPEG", quality=80)
    thumb_b64 = base64.b64encode(thumb_buf.getvalue()).decode("ascii")

    try:
        page_info = await _phase0_scan(thumb_b64)
    except Exception as e:
        logger.warning("Phase 0 failed: %s — using defaults", e)
        page_info = {"body_font": "sans-serif", "body_color": "#000000", "body_bg": "#ffffff"}

    # ── Phase 1: Slice ──
    from app.agents.appbuilder.tools.screenshot_slicer import slice_screenshot
    slices = slice_screenshot(screenshot_b64)
    logger.info("Phase 1: %d slices from %dx%d screenshot",
                len(slices), full_img.width, full_img.height)

    if not slices:
        return ToolResult(success=False, error="Failed to slice screenshot — no sections detected.")

    # ── Phase 2: Per-slice vision build (parallel, max 3 concurrent) ──
    sem = asyncio.Semaphore(3)

    async def build_with_semaphore(sl):
        async with sem:
            return await _phase2_build_slice(
                sl.jpeg_b64, sl.index, len(slices),
                sl.width, sl.height, sl.avg_bg_color, page_info,
            )

    logger.info("Phase 2: Building %d slices (3 concurrent)...", len(slices))
    slice_results = await asyncio.gather(
        *[build_with_semaphore(sl) for sl in slices],
        return_exceptions=True,
    )

    # Merge all slice components and collect assets/fonts
    comp_def: dict[str, Any] = {}
    all_assets: list[dict] = []
    all_fonts: list[dict] = []
    section_keys: list[str] = []

    for i, result in enumerate(slice_results):
        if isinstance(result, Exception):
            logger.warning("Slice %d failed: %s", i, result)
            continue
        components = result.get("components", {})
        comp_def.update(components)
        all_assets.extend(result.get("assets", []))
        all_fonts.extend(result.get("fonts", []))

        # Track section root
        root_key = f"s{i}_sectionRoot"
        if root_key in components:
            section_keys.append(root_key)
        else:
            # Find the first key from this slice
            for k in components:
                if k.startswith(f"s{i}_"):
                    section_keys.append(k)
                    break

    if not section_keys:
        return ToolResult(success=False, error="No sections could be built from the screenshot.")

    # Validate components
    comp_def = _validate_components(comp_def)

    from app.agents.appbuilder.tools._shared import get_saas_client
    # Inject slice images as section background images.
    # Most marketing pages use full-bleed photos as section backgrounds.
    # The per-slice JPEG captures this perfectly — upload it and set as
    # backgroundImage on the section root for a huge visual fidelity boost.
    for sl in slices:
        root_key = f"s{sl.index}_sectionRoot"
        if root_key not in comp_def:
            continue
        # Upload the slice JPEG as a background asset
        slice_bytes = base64.b64decode(sl.jpeg_b64)
        from app.agents.appbuilder.tools.asset_resolver import _upload_to_files
        bg_url = await _upload_to_files(
            slice_bytes, "image/jpeg", app_code, client_code,
            get_saas_client(), headers, {},
        )
        if bg_url:
            # Set as section backgroundImage
            comp = comp_def[root_key]
            sp = comp.get("styleProperties", {})
            if sp:
                first_sid = next(iter(sp))
                all_res = sp[first_sid].setdefault("resolutions", {}).setdefault("ALL", {})
                all_res["backgroundImage"] = {"value": f"url('{bg_url}')"}
                all_res["backgroundSize"] = {"value": "cover"}
                all_res["backgroundPosition"] = {"value": "center top"}
                all_res["backgroundRepeat"] = {"value": "no-repeat"}
                # Scale the slice pixel height to the target viewport (1440px).
                # Screenshots are often retina (2x-3x), so a 3458px-wide
                # capture represents a ~1440px CSS viewport.
                scaled_h = round(sl.height * (1440 / sl.width))
                all_res["minHeight"] = {"value": f"{scaled_h}px"}
                # Remove any solid backgroundColor from the section root
                # (would hide the background image)
                all_res.pop("backgroundColor", None)
                logger.info("Injected slice %d as background on %s (h=%dpx)", sl.index, root_key, scaled_h)

                # Also make direct children transparent so bg image shows through
                for child_key in comp.get("children", {}):
                    child_comp = comp_def.get(child_key)
                    if not child_comp:
                        continue
                    for csid, cst in child_comp.get("styleProperties", {}).items():
                        child_all = cst.get("resolutions", {}).get("ALL", {})
                        bg = child_all.get("backgroundColor", {})
                        bgv = bg.get("value", "") if isinstance(bg, dict) else ""
                        # Only clear opaque backgrounds (not transparent/none)
                        if bgv and bgv not in ("transparent", "none", "rgba(0,0,0,0)", "rgba(0, 0, 0, 0)"):
                            child_all["backgroundColor"] = {"value": "transparent"}

    logger.info("Phase 2 complete: %d components, %d sections, %d assets, %d fonts",
                len(comp_def), len(section_keys), len(all_assets), len(all_fonts))

    # ── Phase 3: Asset resolution ──
    from app.agents.appbuilder.tools.asset_resolver import (
        resolve_assets, detect_icon_packs, rewrite_placeholders,
    )
    from app.agents.appbuilder.tools._shared import get_saas_client

    api_client = get_saas_client()
    slices_y_offsets = {sl.index: sl.y_start for sl in slices}

    logger.info("Phase 3: Resolving %d assets...", len(all_assets))
    resolved = await resolve_assets(
        all_assets, full_img, slices_y_offsets,
        api_client, headers, app_code, client_code,
    )
    rewrite_count = rewrite_placeholders(comp_def, resolved)
    logger.info("Phase 3: Rewrote %d/%d asset placeholders", rewrite_count, len(all_assets))

    # Detect required icon packs
    icon_packs = detect_icon_packs(comp_def)
    logger.info("Phase 3: Icon packs needed: %s", icon_packs)

    # ── Phase 4: Assemble ──
    logger.info("Phase 4: Assembling %d sections under root", len(section_keys))

    body_font = page_info.get("body_font", "")
    body_color = page_info.get("body_color", "")
    body_bg = page_info.get("body_bg", "")

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
    if body_bg and body_bg != "#ffffff":
        root_styles["backgroundColor"] = {"value": body_bg}

    style_id = uuid.uuid4().hex[:22]
    comp_def["root"] = {
        "key": "root",
        "name": "rootGrid",
        "type": "Grid",
        "properties": {"containerType": {"value": "_bare"}},
        "styleProperties": {style_id: {"resolutions": {"ALL": root_styles}}},
        "children": {k: True for k in section_keys},
        "displayOrder": 0,
    }
    for i, key in enumerate(section_keys):
        if key in comp_def:
            comp_def[key]["displayOrder"] = i

    # Font packs
    from app.agents.appbuilder.tools.html_to_modlix import _extract_font_packs
    font_packs = await _extract_font_packs(comp_def)

    logger.info("Phase 4: %d total components, %d font packs, %d icon packs",
                len(comp_def), len(font_packs), len(icon_packs))

    # ── Phase 5: Save ──
    logger.info("Phase 5: Saving page '%s' in app '%s'", page_name, app_code)

    page_def = {
        "name": page_name,
        "appCode": app_code,
        "clientCode": client_code,
        "rootComponent": "root",
        "componentDefinition": comp_def,
        "eventFunctions": {},
        "properties": {},
        "translations": {},
        "message": "Built from screenshot",
    }

    existing_page = None
    if replace_existing:
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
            existing_data = full_result.data
            existing_data["componentDefinition"] = comp_def
            existing_data["rootComponent"] = "root"
            existing_data["message"] = "Built from screenshot"
            save_result = await api_client.put(f"/api/ui/pages/{page_id}", headers=headers, json=existing_data)
            if not save_result.success:
                return ToolResult(success=False, error=f"Failed to update page: {save_result.error}")
    else:
        save_result = await api_client.post("/api/ui/pages", headers=headers, json=page_def)
        if save_result.success:
            page_id = save_result.data.get("id", "?") if isinstance(save_result.data, dict) else "?"
        else:
            return ToolResult(success=False, error=f"Failed to create page: {save_result.error}")

    logger.info("Page saved: id=%s", page_id)

    # Update Application font packs + icon packs
    if font_packs or icon_packs:
        try:
            app_list = await api_client.get(
                "/api/ui/applications", headers=headers,
                params={"page": 0, "size": 1, "appCode": app_code},
            )
            if app_list.success:
                apps = app_list.data.get("content", []) if isinstance(app_list.data, dict) else []
                if apps:
                    app_id = apps[0].get("id")
                    app_full = await api_client.get(f"/api/ui/applications/{app_id}", headers=headers)
                    if app_full.success:
                        app_data = app_full.data
                        # Font packs
                        if font_packs:
                            existing_packs = app_data.get("properties", {}).get("fontPacks", {})
                            existing_names = {fp.get("name", "").lower() for fp in existing_packs.values()}
                            new_packs = {k: v for k, v in font_packs.items() if v["name"].lower() not in existing_names}
                            if new_packs:
                                app_data.setdefault("properties", {}).setdefault("fontPacks", {}).update(new_packs)
                        # Icon packs
                        if icon_packs:
                            existing_ipacks = app_data.get("properties", {}).get("iconPacks", {})
                            existing_inames = {ip.get("name", "") for ip in existing_ipacks.values()}
                            for pack_name in icon_packs:
                                if pack_name not in existing_inames:
                                    pk = uuid.uuid4().hex[:22]
                                    app_data.setdefault("properties", {}).setdefault("iconPacks", {})[pk] = {"name": pack_name}
                        app_data["message"] = f"Updated packs for screenshot-built page '{page_name}'"
                        await api_client.put(f"/api/ui/applications/{app_id}", headers=headers, json=app_data)
                        logger.info("Updated Application with font/icon packs")
        except Exception as e:
            logger.warning("Pack update failed: %s", e)

    # ── Phase 6: QA ──
    qa_summary = ""
    try:
        referer = context.get("referer", "")
        if referer:
            from urllib.parse import urlparse
            parsed = urlparse(referer)
            base_url = f"{parsed.scheme}://{parsed.netloc}"
        else:
            auth = context.get("headers", {})
            fh = auth.get("X-Forwarded-Host", "apps.local.modlix.com").split(",")[0].strip()
            scheme = "http" if "localhost" in fh or "127.0.0.1" in fh else "https"
            base_url = f"{scheme}://{fh}"

        generated_url = f"{base_url}/{app_code}/{client_code}/page/{page_name}"

        from app.agents.appbuilder.tools.visual_qa import (
            take_multi_viewport_screenshots, compute_similarity, VIEWPORTS,
        )

        gen_ss = await take_multi_viewport_screenshots(generated_url)
        if gen_ss:
            scores = {}
            # Compare against user screenshot (desktop only for v1)
            user_desktop_b64 = screenshot_b64
            gen_desktop = gen_ss.get("desktop")
            if gen_desktop:
                scores["desktop"] = compute_similarity(user_desktop_b64, gen_desktop)
            if scores:
                avg = sum(scores.values()) / len(scores)
                per_vp = " ".join(f"{k}={v:.1f}%" for k, v in scores.items())
                qa_summary = f"\nSimilarity: avg={avg:.1f}% ({per_vp})"
                logger.info("Phase 6 QA: %s", qa_summary.strip())
    except Exception as e:
        logger.warning("Phase 6 QA failed: %s", e)
        qa_summary = f"\nQA: skipped ({e})"

    # Clean up session context
    session_ctx.pop("user_screenshot_b64", None)
    session_ctx.pop("user_screenshot_mime", None)

    return ToolResult(
        success=True,
        summary=(
            f"{'Updated' if existing_page else 'Created'} page '{page_name}' "
            f"with {len(comp_def)} components ({len(section_keys)} sections) "
            f"built from screenshot. "
            f"{rewrite_count} assets resolved, "
            f"{len(icon_packs)} icon packs, {len(font_packs)} font packs."
            f"{qa_summary}\n"
            f"Page ID: {page_id}"
        ),
        result_tier=ResultTier.COMPACT,
    )


# ── Tool Definition ────────────────────────────────────────────────

BUILD_PAGE_FROM_SCREENSHOT = ToolDefinition(
    name="build_page_from_screenshot",
    display_name="Build Page From Screenshot",
    description=(
        "Build a Modlix page from a screenshot/image attached to this conversation. "
        "Slices the screenshot into sections, uses vision AI to generate Modlix components "
        "for each section, crops images/logos from the screenshot and uploads them to the "
        "files service, detects fonts and colors, registers icon packs. "
        "Use this when the user pastes an image and asks to build/clone/recreate a page from it."
    ),
    parameters=[
        ToolParameter(name="page_name", type="string", description="Name for the page.", required=True),
        ToolParameter(name="app_code", type="string", description="Application code.", required=False),
        ToolParameter(name="replace_existing", type="boolean", description="Replace existing page with same name (default true).", required=False),
    ],
    execute=_execute_build_from_screenshot,
    is_deferred=True,
    search_hint="screenshot image picture paste photo mockup design build page from image visual",
    result_tier=ResultTier.COMPACT,
)
