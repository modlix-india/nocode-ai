"""Visual QA — screenshot comparison and iterative fix loop.

Takes screenshots of the generated page at multiple viewports,
compares with source screenshots, and uses Gemini Flash to identify
differences and generate fixes.

Pipeline:
1. Screenshot source URL at desktop/tablet/mobile
2. Screenshot generated page at same viewports
3. Gemini Flash compares pairs and lists differences
4. Generate Modlix component updates to fix differences
5. Apply fixes and repeat
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Viewport sizes for comparison
VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "tablet": {"width": 1024, "height": 768},
    "mobile": {"width": 375, "height": 812},
}


async def take_multi_viewport_screenshots(url: str) -> dict[str, str]:
    """Take screenshots at multiple viewport sizes.

    Returns dict of viewport_name → base64 PNG.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {}

    screenshots: dict[str, str] = {}

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)

            for vp_name, vp_size in VIEWPORTS.items():
                try:
                    page = await browser.new_page(viewport=vp_size)
                    await page.goto(url, wait_until="networkidle", timeout=20000)
                    await asyncio.sleep(1)
                    screenshot_bytes = await page.screenshot(full_page=True)
                    screenshots[vp_name] = base64.b64encode(screenshot_bytes).decode("ascii")
                    await page.close()
                    logger.info("Screenshot %s for %s: %d bytes", vp_name, url, len(screenshot_bytes))
                except Exception as e:
                    logger.warning("Screenshot %s failed for %s: %s", vp_name, url, e)

            await browser.close()
    except Exception as e:
        logger.warning("Multi-viewport screenshots failed: %s", e)

    return screenshots


_COMPARE_PROMPT = """\
Compare these two website screenshots. The FIRST image is the SOURCE (target design).
The SECOND image is the GENERATED page that should match the source.

Identify ALL visual differences and return a JSON array of specific fixes.
Focus on:

1. **Layout issues**: Elements that should be side-by-side but are stacked vertically.
   Fix with: {"key": "grid_key", "action": "set_property", "properties": {"layout": "ROWLAYOUT"}}

2. **Missing backgrounds**: Sections that should have background images/colors but don't.
   Fix with: {"key": "comp_key", "action": "set_style", "styles": {"backgroundImage": "url('...')", "backgroundSize": "cover"}}

3. **Wrong colors**: Text or background colors that don't match.
   Fix with: {"key": "comp_key", "action": "set_style", "styles": {"color": "#hex", "backgroundColor": "#hex"}}

4. **Sizing issues**: Elements too big, too small, wrong height.
   Fix with: {"key": "comp_key", "action": "set_style", "styles": {"height": "500px", "width": "100%"}}

5. **Spacing issues**: Too much or too little padding/margin.
   Fix with: {"key": "comp_key", "action": "set_style", "styles": {"paddingTop": "40px"}}

6. **Missing elements**: Elements visible in source but not in generated.
   Note these but don't try to create them.

7. **Typography**: Wrong font sizes, weights, or families.
   Fix with: {"key": "comp_key", "action": "set_style", "styles": {"fontSize": "24px", "fontWeight": "700"}}

IMPORTANT: Grid components are flex containers with DEFAULT COLUMN direction.
To make children horizontal, set layout property to "ROWLAYOUT":
{"key": "grid_key", "action": "set_property", "properties": {"layout": "ROWLAYOUT"}}

Here is the component tree of the generated page for reference:
"""


async def compare_and_fix(
    source_screenshot: str,
    generated_screenshot: str,
    comp_def: dict[str, Any],
    viewport: str = "desktop",
) -> list[dict[str, Any]]:
    """Compare source vs generated screenshots and return fixes.

    Args:
        source_screenshot: Base64 PNG of source website.
        generated_screenshot: Base64 PNG of generated page.
        comp_def: Current componentDefinition for context.
        viewport: Which viewport this comparison is for.

    Returns:
        List of fix dicts to apply.
    """
    try:
        import google.generativeai as genai
        from app.config import settings

        if not settings.GOOGLE_API_KEY:
            return []

        genai.configure(api_key=settings.GOOGLE_API_KEY)
        model = genai.GenerativeModel("gemini-2.0-flash")
    except (ImportError, Exception) as e:
        logger.warning("Cannot run visual comparison: %s", e)
        return []

    # Build compact tree for context
    from app.agents.appbuilder.tools.layout_refiner import _build_compact_tree
    tree_text = _build_compact_tree(comp_def, max_depth=2)

    source_bytes = base64.b64decode(source_screenshot)
    generated_bytes = base64.b64decode(generated_screenshot)

    prompt = (
        f"Viewport: {viewport}\n\n"
        + _COMPARE_PROMPT
        + tree_text
        + "\n\nReturn ONLY a JSON array of fixes. No explanation."
    )

    try:
        response = await asyncio.to_thread(
            model.generate_content,
            [
                {"mime_type": "image/png", "data": source_bytes},
                {"mime_type": "image/png", "data": generated_bytes},
                prompt,
            ],
        )

        response_text = response.text or "[]"
        # Extract JSON
        import re
        if "```" in response_text:
            match = re.search(r"```(?:json)?\s*([\s\S]*?)```", response_text)
            if match:
                response_text = match.group(1)

        fixes = json.loads(response_text)
        if not isinstance(fixes, list):
            fixes = [fixes]

        logger.info("Visual QA (%s): %d fixes suggested", viewport, len(fixes))
        return fixes

    except Exception as e:
        logger.warning("Visual comparison failed (%s): %s", viewport, e)
        return []


async def iterative_visual_fix(
    source_url: str,
    generated_page_url: str,
    comp_def: dict[str, Any],
    max_iterations: int = 2,
) -> list[dict[str, Any]]:
    """Run iterative visual QA: screenshot → compare → fix → repeat.

    Args:
        source_url: URL of the source website.
        generated_page_url: URL of the generated Modlix page.
        comp_def: componentDefinition dict (modified in place).
        max_iterations: Max fix iterations.

    Returns:
        All fixes applied across iterations.
    """
    all_fixes: list[dict[str, Any]] = []

    # Screenshot source once
    source_screenshots = await take_multi_viewport_screenshots(source_url)
    if not source_screenshots:
        logger.warning("Cannot take source screenshots — skipping visual QA")
        return []

    from app.agents.appbuilder.tools.layout_refiner import _apply_fixes

    for iteration in range(max_iterations):
        logger.info("Visual QA iteration %d/%d", iteration + 1, max_iterations)

        # Screenshot generated page
        gen_screenshots = await take_multi_viewport_screenshots(generated_page_url)
        if not gen_screenshots:
            break

        iteration_fixes = []

        # Compare at each viewport
        for vp_name in VIEWPORTS:
            source_ss = source_screenshots.get(vp_name)
            gen_ss = gen_screenshots.get(vp_name)
            if not source_ss or not gen_ss:
                continue

            fixes = await compare_and_fix(source_ss, gen_ss, comp_def, vp_name)
            iteration_fixes.extend(fixes)

        if not iteration_fixes:
            logger.info("No more fixes needed — visual QA complete")
            break

        # Apply fixes
        applied = _apply_fixes(comp_def, iteration_fixes)
        all_fixes.extend(applied)
        logger.info("Iteration %d: applied %d fixes", iteration + 1, len(applied))

    return all_fixes
