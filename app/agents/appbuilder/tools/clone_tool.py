"""clone_website tool — spec-driven, section-by-section website cloning.

Pipeline:
1. Extract: Playwright discovers sections, extracts per-element CSS, assets, content
2. Foundation: Create root component, resolve fonts, set up Application
3. Build: LLM generates Modlix JSON per section from detailed specs
4. Assemble: Combine sections, save page + fonts
5. QA: Screenshot comparison, re-generate broken sections

Adapted from ai-website-cloner-template methodology.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.core.tools.base import (
    ToolDefinition,
    ToolParameter,
    ToolResult,
    ResultTier,
)

logger = logging.getLogger(__name__)


async def _execute_clone_website(
    params: dict[str, Any], context: dict[str, Any],
) -> ToolResult:
    """Clone a website using the spec-driven section-by-section pipeline."""
    url = params.get("url", "")
    page_name = params.get("page_name", "home").lower()
    app_code = params.get("app_code") or context.get("app_code", "")
    replace_existing = params.get("replace_existing", True)

    if not url:
        return ToolResult(success=False, error="url is required.")
    if not app_code:
        return ToolResult(success=False, error="app_code is required.")

    client_code = context.get("client_code", "SYSTEM")

    # ── Phase 1: Extraction ──
    from app.agents.appbuilder.tools.section_extractor import extract_page

    try:
        logger.info("Phase 1: Extracting %s", url)
        extraction = await extract_page(url)
        logger.info("Extracted %d sections, %d fonts from %s",
                     len(extraction.sections), len(extraction.all_fonts), url)
    except Exception as e:
        logger.error("Phase 1 failed: %s", e)
        return ToolResult(success=False, error=f"Extraction failed for {url}: {e}")

    if not extraction.sections:
        return ToolResult(success=False, error=f"No sections found on {url}")

    # ── Phase 2: Foundation ──
    logger.info("Phase 2: Building foundation (root + fonts)")

    # Resolve fonts
    from app.agents.appbuilder.tools.html_to_modlix import (
        _extract_font_packs, _FONT_REPLACEMENTS, _KNOWN_GOOGLE_FONTS, _SYSTEM_FONTS,
    )

    # Build a temporary comp_def with font info for _extract_font_packs
    temp_comp = {}
    body_font = extraction.body_font
    resolved_body_font = body_font

    # Resolve body font
    if body_font:
        lower = body_font.lower()
        if lower in _KNOWN_GOOGLE_FONTS:
            resolved_body_font = body_font
        elif lower in _FONT_REPLACEMENTS:
            resolved_body_font = _FONT_REPLACEMENTS[lower]
            logger.info("Body font '%s' → '%s'", body_font, resolved_body_font)
        elif lower not in _SYSTEM_FONTS:
            # LLM fallback
            from app.agents.appbuilder.tools.html_to_modlix import _resolve_font_with_llm
            suggestion = await _resolve_font_with_llm(body_font)
            if suggestion:
                resolved_body_font = suggestion
                logger.info("Body font '%s' → '%s' (LLM)", body_font, resolved_body_font)

    # Build root component — let content flow naturally with page scroll
    # instead of creating an inner-scrolling container (matches source behavior).
    # align-items: center centers any fixed-width section (e.g. <main max-width:720px>)
    # whose authored `margin: 0 auto` centering was lost when the SPA unwrap
    # drilled through its wrapper. Sections with width:100% are unaffected.
    root_styles = {
        "width": {"value": "100%"},
        "display": {"value": "flex"},
        "flexDirection": {"value": "column"},
        "alignItems": {"value": "center"},
    }
    root_properties: dict[str, Any] = {
        "containerType": {"value": "_bare"},
    }
    if resolved_body_font:
        root_styles["fontFamily"] = {"value": resolved_body_font}
    if extraction.body_color and extraction.body_color != "rgb(0, 0, 0)":
        root_styles["color"] = {"value": extraction.body_color}

    comp_def: dict[str, Any] = {}

    # ── Phase 3: Build each section ──
    from app.agents.appbuilder.tools.section_builder import build_section

    section_keys: list[str] = []
    for i, spec in enumerate(extraction.sections):
        section_key = f"{spec.name}Section"
        logger.info("Phase 3: Building section %d/%d: '%s' (%d texts, %d images)",
                     i + 1, len(extraction.sections), spec.name,
                     len(spec.content_text), len(spec.images))
        try:
            section_comp = await build_section(spec, body_font=resolved_body_font)
            # Merge into main comp_def
            comp_def.update(section_comp)
            # Track the top-level key for this section
            if section_key in section_comp:
                section_keys.append(section_key)
            else:
                # Find the top-level key (component with no parent in this section)
                child_keys = set()
                for c in section_comp.values():
                    for ck in c.get("children", {}):
                        child_keys.add(ck)
                top_keys = [k for k in section_comp if k not in child_keys]
                if top_keys:
                    section_keys.append(top_keys[0])

            logger.info("  Section '%s': %d components generated", spec.name, len(section_comp))

        except Exception as e:
            logger.warning("  Section '%s' build failed: %s — skipping", spec.name, e)

    if not section_keys:
        return ToolResult(success=False, error="No sections could be built")

    # ── Phase 4: Assembly ──
    logger.info("Phase 4: Assembling %d sections under root", len(section_keys))

    # Create root
    style_id = uuid.uuid4().hex[:22]
    comp_def["root"] = {
        "key": "root",
        "name": "rootGrid",
        "type": "Grid",
        "properties": root_properties,
        "styleProperties": {style_id: {"resolutions": {"ALL": root_styles}}},
        "children": {k: True for k in section_keys},
        "displayOrder": 0,
    }

    # Set displayOrder on sections
    for i, key in enumerate(section_keys):
        if key in comp_def:
            comp_def[key]["displayOrder"] = i

    logger.info("Total: %d components, %d sections", len(comp_def), len(section_keys))

    # Build font packs from all components
    font_packs = await _extract_font_packs(comp_def)

    # Keyframes emission disabled — Modlix's processStyleFromString doesn't
    # accept the raw "0% { ... } 100% { ... }" body we extract; crashes page
    # render with `undefined.toUpperCase()`. Needs a different data format
    # or pre-parsed structure. Leaving extraction in place.
    page_classes: dict[str, Any] = {}
    page_properties: dict[str, Any] = {}

    # Build page definition
    page_def = {
        "name": page_name,
        "appCode": app_code,
        "clientCode": client_code,
        "rootComponent": "root",
        "componentDefinition": comp_def,
        "eventFunctions": {},
        "properties": page_properties,
        "translations": {},
        "message": f"Cloned from {url} (spec-driven)",
    }

    # Save page
    from app.agents.appbuilder.tools._shared import get_saas_client
    api_client = get_saas_client()
    headers = context["headers"]

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
            existing_data["message"] = f"Cloned from {url}"
            # Merge keyframes into existing page.properties.classes (replace our
            # previously-added @keyframes but leave unrelated classes alone).
            existing_props = existing_data.setdefault("properties", {})
            existing_classes = existing_props.get("classes", {}) or {}
            # Drop old @keyframes-* entries we added before
            existing_classes = {
                k: v for k, v in existing_classes.items()
                if not str(v.get("selector", "")).startswith("@keyframes ")
            }
            existing_classes.update(page_classes)
            if existing_classes:
                existing_props["classes"] = existing_classes
            else:
                existing_props.pop("classes", None)
            save_result = await api_client.put(f"/api/ui/pages/{page_id}", headers=headers, json=existing_data)
            if not save_result.success:
                return ToolResult(success=False, error=f"Failed to update page: {save_result.error}")
        else:
            return ToolResult(success=False, error=f"Failed to read existing page: {full_result.error}")
    else:
        save_result = await api_client.post("/api/ui/pages", headers=headers, json=page_def)
        if save_result.success:
            page_id = save_result.data.get("id", "?") if isinstance(save_result.data, dict) else "?"
        else:
            return ToolResult(success=False, error=f"Failed to create page: {save_result.error}")

    logger.info("Page saved: id=%s", page_id)

    # Update Application fontPacks
    if font_packs:
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
                        existing_packs = app_data.get("properties", {}).get("fontPacks", {})
                        existing_names = {fp.get("name", "").lower() for fp in existing_packs.values()}
                        new_packs = {k: v for k, v in font_packs.items() if v["name"].lower() not in existing_names}
                        if new_packs:
                            app_data.setdefault("properties", {}).setdefault("fontPacks", {}).update(new_packs)
                            app_data["message"] = f"Added fonts: {', '.join(p['name'] for p in new_packs.values())}"
                            await api_client.put(f"/api/ui/applications/{app_id}", headers=headers, json=app_data)
                            logger.info("Added font packs: %s", [p["name"] for p in new_packs.values()])
        except Exception as e:
            logger.warning("Font pack update failed: %s", e)

    # ── Phase 5: Single-shot Visual QA (no LLM fixes) ──
    # LLM-based QA fixes compound bad suggestions (e.g. `background:` shorthand
    # wiping backgroundImage) and the pixel-similarity signal is unreliable for
    # rollback decisions. Relying on deterministic extraction+build instead.
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

        async def save_page_update() -> bool:
            """Save current comp_def to API."""
            if not page_id:
                return False
            rd = await api_client.get(f"/api/ui/pages/{page_id}", headers=headers)
            if not rd.success:
                return False
            data = rd.data
            data["componentDefinition"] = comp_def
            data["message"] = "Visual QA fix"
            sv = await api_client.put(f"/api/ui/pages/{page_id}", headers=headers, json=data)
            return sv.success

        # Screenshot source + gen once for a similarity snapshot (no fixes applied)
        source_ss = await take_multi_viewport_screenshots(url)
        gen_ss_once = await take_multi_viewport_screenshots(generated_url)
        if source_ss and gen_ss_once:
            scores = {}
            for vp in VIEWPORTS:
                s = source_ss.get(vp); g = gen_ss_once.get(vp)
                if s and g:
                    scores[vp] = compute_similarity(s, g)
            if scores:
                avg = sum(scores.values()) / len(scores)
                per_vp = " ".join(f"{k}={v:.1f}%" for k, v in scores.items())
                qa_summary = (
                    f"\nSimilarity: avg={avg:.1f}% ({per_vp})"
                )
                logger.info("Final similarity avg=%.1f%% %s", avg, scores)

        # LLM QA loop disabled — see note above.

        if not qa_summary:
            qa_summary = "\nSimilarity: not computed"

    except Exception as e:
        logger.warning("Visual QA failed: %s", e)
        qa_summary = f"\nVisual QA: skipped ({e})"

    comp_count = len(comp_def)
    return ToolResult(
        success=True,
        summary=(
            f"{'Updated' if existing_page else 'Created'} page '{page_name}' "
            f"with {comp_count} components ({len(section_keys)} sections) cloned from {url}.{qa_summary}\n"
            f"Page ID: {page_id}\n"
            f"Sections: {', '.join(section_keys)}"
        ),
        result_tier=ResultTier.COMPACT,
    )


CLONE_WEBSITE = ToolDefinition(
    name="clone_website",
    display_name="Clone Website",
    description=(
        "Clone a website by URL — extracts sections with exact CSS, then generates "
        "Modlix components per section using AI vision. Handles layout, images, "
        "text, navigation, fonts, and responsive styles. Use this instead of manually "
        "building components when cloning an existing website."
    ),
    parameters=[
        ToolParameter(name="url", type="string", description="URL to clone.", required=True),
        ToolParameter(name="page_name", type="string", description="Name for the page (default 'home').", required=True),
        ToolParameter(name="app_code", type="string", description="Application code.", required=False),
        ToolParameter(name="replace_existing", type="boolean", description="Replace existing page with same name (default true).", required=False),
    ],
    execute=_execute_clone_website,
    is_deferred=True,
    search_hint="clone website scrape HTML convert copy duplicate URL page",
    result_tier=ResultTier.COMPACT,
)
