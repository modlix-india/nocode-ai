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


async def _modlix_fast_clone(
    modlix_data: Any,
    page_name: str,
    app_code: str,
    client_code: str,
    headers: dict[str, str],
    replace_existing: bool,
    source_url: str,
) -> ToolResult | None:
    """Fast-path clone for Modlix-built sites.

    Extracts componentDefinition directly from getStore().pageDefinition —
    100% fidelity clone with no AI generation needed.  Re-uploads any
    images that reference the source domain to our files service.

    Returns a ToolResult on success, or None to fall through to the
    normal clone pipeline.
    """
    import re as _re

    from app.agents.appbuilder.tools._shared import get_saas_client
    from app.agents.appbuilder.tools.asset_resolver import _upload_to_files
    from app.agents.appbuilder.tools.html_to_modlix import _extract_font_packs

    comp_def = modlix_data.component_definition
    if not comp_def:
        return None

    api_client = get_saas_client()
    img_cache: dict[str, str] = {}
    rewrite_count = 0

    # ── Re-upload images from source domain to our files service ──
    # Walk all components, find Image src and backgroundImage values
    # that point to external URLs, download and re-upload them.
    import httpx

    async def _download_and_reupload(src_url: str) -> str | None:
        if not src_url or src_url.startswith("data:"):
            return None
        # Skip URLs already on our files service
        if src_url.startswith("/api/files") or "api/files" in src_url:
            return None
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(src_url)
                if resp.status_code != 200 or len(resp.content) < 100:
                    return None
                ct = resp.headers.get("content-type", "image/png")
                mime = ct.split(";")[0].strip()
                if "image" not in mime and "svg" not in mime:
                    return None
                return await _upload_to_files(
                    resp.content, mime, app_code, client_code,
                    api_client, headers, img_cache, page_name=page_name,
                )
        except Exception as e:
            logger.debug("Image re-upload failed for %s: %s", src_url[:80], e)
            return None

    # Rewrite Image component src values
    for key, comp in comp_def.items():
        if comp.get("type") == "Image":
            src = comp.get("properties", {}).get("src", {}).get("value", "")
            if src and (src.startswith("http://") or src.startswith("https://")):
                new_url = await _download_and_reupload(src)
                if new_url:
                    comp["properties"]["src"]["value"] = new_url
                    rewrite_count += 1

        # Rewrite backgroundImage URLs in styleProperties
        for sid, st in comp.get("styleProperties", {}).items():
            for res_name, res in st.get("resolutions", {}).items():
                bg = res.get("backgroundImage", {})
                bgv = bg.get("value", "") if isinstance(bg, dict) else ""
                if bgv and "url(" in bgv:
                    m = _re.search(r"url\(['\"]?(https?://[^'\")\s]+)['\"]?\)", bgv)
                    if m:
                        new_url = await _download_and_reupload(m.group(1))
                        if new_url:
                            bg["value"] = f"url('{new_url}')"
                            rewrite_count += 1

    logger.info("Modlix fast-clone: re-uploaded %d images (%d unique)",
                rewrite_count, len(img_cache))

    # ── Extract font packs ──
    font_packs = await _extract_font_packs(comp_def)

    # ── Save page ──
    page_def = {
        "name": page_name,
        "appCode": app_code,
        "clientCode": client_code,
        "rootComponent": modlix_data.root_component,
        "componentDefinition": comp_def,
        "eventFunctions": modlix_data.event_functions,
        "properties": modlix_data.properties,
        "translations": modlix_data.translations,
        "message": f"Modlix clone from {source_url} (page: {modlix_data.page_name})",
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
            data = full_result.data
            data["componentDefinition"] = comp_def
            data["rootComponent"] = modlix_data.root_component
            data["eventFunctions"] = modlix_data.event_functions
            data["message"] = f"Modlix clone from {source_url}"
            save_result = await api_client.put(f"/api/ui/pages/{page_id}", headers=headers, json=data)
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

    # ── Update font packs on Application ──
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
                        new_packs = {k: v for k, v in font_packs.items()
                                     if v["name"].lower() not in existing_names}
                        if new_packs:
                            app_data.setdefault("properties", {}).setdefault("fontPacks", {}).update(new_packs)
                            app_data["message"] = f"Fonts from Modlix clone of {source_url}"
                            await api_client.put(f"/api/ui/applications/{app_id}", headers=headers, json=app_data)
        except Exception as e:
            logger.warning("Font pack update failed: %s", e)

    comp_count = len(comp_def)
    return ToolResult(
        success=True,
        summary=(
            f"{'Updated' if existing_page else 'Created'} page '{page_name}' "
            f"with {comp_count} components — DIRECT Modlix clone from {source_url} "
            f"(source page: '{modlix_data.page_name}', 100% fidelity). "
            f"{rewrite_count} images re-uploaded.\n"
            f"Page ID: {page_id}"
        ),
        result_tier=ResultTier.COMPACT,
    )


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
    headers = context["headers"]

    # ── Phase 0: Modlix site detection ──
    # If the target site is built on Modlix, we can extract the page JSON
    # directly from getStore().pageDefinition — 100% fidelity, no AI needed.
    from app.agents.appbuilder.tools._shared import detect_modlix_site
    logger.info("Phase 0: Checking if %s is a Modlix site...", url)
    modlix_data = await detect_modlix_site(url)
    if modlix_data:
        logger.info("Modlix site detected! Fast-path clone of page '%s' (%d components)",
                     modlix_data.page_name, len(modlix_data.component_definition))
        result = await _modlix_fast_clone(
            modlix_data, page_name, app_code, client_code, headers,
            replace_existing, url,
        )
        if result is not None:
            return result
        # If fast clone returned None, fall through to normal pipeline
        logger.info("Modlix fast-clone returned None, falling through to normal pipeline")

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

    # ── Download external images and re-upload to Modlix files service ──
    # This makes the cloned page self-contained — no external image dependencies.
    from app.agents.appbuilder.tools._shared import get_saas_client as _get_client
    from app.agents.appbuilder.tools.asset_resolver import _upload_to_files
    _api = _get_client()
    _headers = context["headers"]
    _img_cache: dict[str, str] = {}
    _rewrite_count = 0

    async def _upload_svg_data_uri(data_uri: str) -> str | None:
        """Upload an inline SVG data URI to the files service and return the URL."""
        if not data_uri.startswith("data:image/svg"):
            return None
        try:
            import base64 as _b64
            # data:image/svg+xml;base64,XXXX
            header, payload = data_uri.split(",", 1)
            if "base64" in header:
                svg_bytes = _b64.b64decode(payload)
            else:
                from urllib.parse import unquote
                svg_bytes = unquote(payload).encode("utf-8")
            if len(svg_bytes) < 10:
                return None
            uploaded = await _upload_to_files(
                svg_bytes, "image/svg+xml", app_code, client_code,
                _api, _headers, _img_cache, page_name=page_name,
            )
            return uploaded
        except Exception as e:
            logger.debug("SVG data URI upload failed: %s", e)
            return None

    async def _download_and_reupload(src_url: str) -> str | None:
        """Download an image from an external URL and upload to files service."""
        if not src_url or src_url.startswith("api/files") or src_url.startswith("/api/files"):
            return None
        if src_url.startswith("data:"):
            return await _upload_svg_data_uri(src_url)
        import httpx as _httpx
        try:
            async with _httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(src_url)
                if resp.status_code != 200 or len(resp.content) < 100:
                    return None
                ct = resp.headers.get("content-type", "image/png")
                mime = ct.split(";")[0].strip()
                if "image" not in mime and "svg" not in mime:
                    return None
                uploaded = await _upload_to_files(
                    resp.content, mime, app_code, client_code,
                    _api, _headers, _img_cache, page_name=page_name,
                )
                return uploaded
        except Exception as e:
            logger.debug("Image download failed for %s: %s", src_url[:80], e)
            return None

    # Scan all Image components and rewrite external URLs
    for key, comp in comp_def.items():
        if comp.get("type") != "Image":
            continue
        src = comp.get("properties", {}).get("src", {}).get("value", "")
        if src and (src.startswith("http://") or src.startswith("https://")):
            new_url = await _download_and_reupload(src)
            if new_url:
                comp["properties"]["src"]["value"] = new_url
                _rewrite_count += 1

    # Also rewrite backgroundImage URLs in all components
    for key, comp in comp_def.items():
        for sid, st in comp.get("styleProperties", {}).items():
            for res_name, res in st.get("resolutions", {}).items():
                bg = res.get("backgroundImage", {})
                bgv = bg.get("value", "") if isinstance(bg, dict) else ""
                if bgv and "url(" in bgv:
                    import re as _re
                    m = _re.search(r"url\(['\"]?(https?://[^'\")\s]+)['\"]?\)", bgv)
                    if m:
                        ext_url = m.group(1)
                        new_url = await _download_and_reupload(ext_url)
                        if new_url:
                            bg["value"] = f"url('{new_url}')"
                            _rewrite_count += 1

    if _rewrite_count > 0:
        logger.info("Re-uploaded %d external images to files service (%d unique)", _rewrite_count, len(_img_cache))

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
