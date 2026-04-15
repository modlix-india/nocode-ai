"""Visual QA — screenshot comparison, similarity scoring, and iterative fix loop.

Takes screenshots of the generated page at multiple viewports,
computes pixel similarity against source, uses Gemini Flash to identify
specific differences, applies fixes, SAVES to API, and repeats until
similarity reaches threshold or plateaus.

Pipeline:
1. Screenshot source URL at desktop/tablet/mobile
2. Screenshot generated page at same viewports
3. Compute pixel similarity score (0-100%)
4. Gemini Flash compares pairs and lists differences with component keys
5. Apply fixes to componentDefinition
6. Save updated page via API
7. Repeat until similarity >= threshold or no improvement
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

# Viewport sizes for comparison
VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "tablet": {"width": 1024, "height": 768},
    "mobile": {"width": 375, "height": 812},
}

SIMILARITY_TARGET = 85  # Stop when we reach this %
MAX_ITERATIONS = 3


async def take_multi_viewport_screenshots(url: str, timeout: int = 25000) -> dict[str, str]:
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
                    try:
                        await page.goto(url, wait_until="networkidle", timeout=15000)
                    except Exception:
                        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    await asyncio.sleep(2)
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


def compute_similarity(source_b64: str, generated_b64: str) -> float:
    """Compute visual similarity between two screenshots (0-100%).

    Uses pixel-level comparison with resizing to common dimensions.
    Falls back to file-size heuristic if PIL is unavailable.
    """
    try:
        from PIL import Image
        import numpy as np

        src_img = Image.open(io.BytesIO(base64.b64decode(source_b64))).convert("RGB")
        gen_img = Image.open(io.BytesIO(base64.b64decode(generated_b64))).convert("RGB")

        # Resize to common dimensions (smaller of the two heights, same width).
        # Resize both images to the SAME width (preserving aspect ratio),
        # then crop to the shorter of the two heights (capped at 8000px).
        # This avoids distortion from different source widths (e.g. retina 2x).
        target_w = min(src_img.width, gen_img.width)

        # Resize preserving aspect ratio to target_w
        src_ratio = target_w / src_img.width
        src_resized = src_img.resize(
            (target_w, round(src_img.height * src_ratio)), Image.LANCZOS,
        )
        gen_ratio = target_w / gen_img.width
        gen_resized = gen_img.resize(
            (target_w, round(gen_img.height * gen_ratio)), Image.LANCZOS,
        )

        # Crop both to the same height (shorter of the two, capped at 8000px)
        target_h = min(src_resized.height, gen_resized.height, 8000)
        src_resized = src_resized.crop((0, 0, target_w, target_h))
        gen_resized = gen_resized.crop((0, 0, target_w, target_h))

        src_arr = np.array(src_resized, dtype=np.float32)
        gen_arr = np.array(gen_resized, dtype=np.float32)

        # Mean Structural Similarity approximation
        # Normalized pixel difference
        diff = np.abs(src_arr - gen_arr)
        max_diff = 255.0 * 3  # max possible per-pixel diff (R+G+B)
        pixel_similarity = 1.0 - (diff.sum(axis=2).mean() / max_diff)

        return round(pixel_similarity * 100, 1)

    except ImportError:
        # Fallback: rough size-based heuristic
        src_size = len(source_b64)
        gen_size = len(generated_b64)
        ratio = min(src_size, gen_size) / max(src_size, gen_size)
        return round(ratio * 100, 1)


_COMPARE_PROMPT = """\
You are a precise visual QA engineer. Compare these two website screenshots.
IMAGE 1 = SOURCE (the target design we want to match).
IMAGE 2 = GENERATED (the current page that needs fixes).

FIRST, estimate the overall visual similarity as a percentage (0-100%).

THEN, list EVERY visual difference you can see, from most impactful to least.
For each difference, provide a specific fix using the component keys from the tree below.

Fix format — return a JSON object with two fields:
{
  "similarity_pct": 65,
  "fixes": [
    {"key": "exact_component_key", "action": "set_style", "styles": {"cssProperty": "value"}},
    {"key": "exact_component_key", "action": "set_property", "properties": {"propName": "value"}}
  ]
}

Available actions:
- "set_style": Set CSS properties. Use {"key": "k", "action": "set_style", "styles": {"backgroundColor": "#hex", ...}}
- "set_property": Set component properties. For horizontal layout: {"key": "k", "action": "set_property", "properties": {"layout": "ROWLAYOUT"}}
- "remove": Remove a redundant wrapper. Children get reparented to parent.

Common fixes:
- Horizontal layout: {"action": "set_property", "properties": {"layout": "ROWLAYOUT"}}
- Background: {"action": "set_style", "styles": {"backgroundImage": "url('...')", "backgroundSize": "cover"}}
- Sizing: {"action": "set_style", "styles": {"width": "100%", "height": "500px", "minHeight": "100vh"}}
- Position overlay: {"action": "set_style", "styles": {"position": "absolute", "top": "0", "left": "0"}}
- Colors: {"action": "set_style", "styles": {"color": "#hex", "backgroundColor": "#hex"}}
- Spacing: {"action": "set_style", "styles": {"paddingTop": "40px", "gap": "20px"}}
- Typography: {"action": "set_style", "styles": {"fontSize": "24px", "fontWeight": "700"}}

RULES:
- ONLY use keys that exist in the component tree below. Do NOT invent keys.
- Grid default direction is COLUMN. Use layout=ROWLAYOUT for horizontal.
- Be SPECIFIC with values — use exact hex colors, exact pixel sizes from what you see.
- Focus on the TOP differences first (layout, missing sections, wrong backgrounds).

COMPONENT TREE:
"""


async def compare_and_score(
    source_screenshot: str,
    generated_screenshot: str,
    comp_def: dict[str, Any],
    viewport: str = "desktop",
) -> tuple[list[dict[str, Any]], int]:
    """Compare source vs generated screenshots. Returns (fixes, similarity_pct)."""
    try:
        import google.generativeai as genai
        from app.config import settings

        if not settings.GOOGLE_API_KEY:
            return [], 0

        genai.configure(api_key=settings.GOOGLE_API_KEY)
        model = genai.GenerativeModel("gemini-2.0-flash")
    except (ImportError, Exception) as e:
        logger.warning("Cannot run visual comparison: %s", e)
        return [], 0

    # Build component tree with more depth for better key matching
    from app.agents.appbuilder.tools.layout_refiner import _build_compact_tree
    tree_text = _build_compact_tree(comp_def, max_depth=4)

    source_bytes = base64.b64decode(source_screenshot)
    generated_bytes = base64.b64decode(generated_screenshot)

    prompt = (
        f"Viewport: {viewport} ({VIEWPORTS.get(viewport, {}).get('width', '?')}px)\n\n"
        + _COMPARE_PROMPT
        + tree_text
        + "\n\nReturn ONLY the JSON object with similarity_pct and fixes. No explanation."
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

        response_text = response.text or "{}"
        # Extract JSON
        if "```" in response_text:
            match = re.search(r"```(?:json)?\s*([\s\S]*?)```", response_text)
            if match:
                response_text = match.group(1)

        parsed = json.loads(response_text)

        if isinstance(parsed, list):
            # Old format — just a list of fixes
            fixes = parsed
            similarity = 0
        elif isinstance(parsed, dict):
            fixes = parsed.get("fixes", [])
            similarity = parsed.get("similarity_pct", 0)
        else:
            fixes = []
            similarity = 0

        if not isinstance(fixes, list):
            fixes = [fixes]

        logger.info("Visual QA (%s): similarity=%d%%, %d fixes suggested",
                     viewport, similarity, len(fixes))
        return fixes, similarity

    except Exception as e:
        logger.warning("Visual comparison failed (%s): %s", viewport, e)
        return [], 0


# Type for the save callback used by iterative_visual_fix
SaveCallback = Callable[[dict[str, Any]], Awaitable[bool]]


async def iterative_visual_fix(
    source_url: str,
    generated_page_url: str,
    comp_def: dict[str, Any],
    max_iterations: int = MAX_ITERATIONS,
    save_callback: SaveCallback | None = None,
) -> list[dict[str, Any]]:
    """Run iterative visual QA: screenshot → compare → score → fix → SAVE → repeat.

    Args:
        source_url: URL of the source website.
        generated_page_url: URL of the generated Modlix page.
        comp_def: componentDefinition dict (modified in place).
        max_iterations: Max fix iterations.
        save_callback: Async function to save comp_def to API.
            Called after each iteration with the updated comp_def.
            Must return True on success.

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

    prev_similarity = 0.0

    for iteration in range(max_iterations):
        logger.info("Visual QA iteration %d/%d", iteration + 1, max_iterations)

        # Screenshot generated page (reflects last save)
        gen_screenshots = await take_multi_viewport_screenshots(generated_page_url)
        if not gen_screenshots:
            break

        # Compute pixel similarity per viewport
        viewport_scores: dict[str, float] = {}
        for vp_name in VIEWPORTS:
            source_ss = source_screenshots.get(vp_name)
            gen_ss = gen_screenshots.get(vp_name)
            if source_ss and gen_ss:
                score = compute_similarity(source_ss, gen_ss)
                viewport_scores[vp_name] = score

        avg_similarity = sum(viewport_scores.values()) / max(len(viewport_scores), 1)
        logger.info(
            "Iteration %d similarity: avg=%.1f%% %s",
            iteration + 1, avg_similarity,
            {k: f"{v:.1f}%" for k, v in viewport_scores.items()},
        )

        # Check if we've reached the target
        if avg_similarity >= SIMILARITY_TARGET:
            logger.info("Reached %.1f%% similarity (target %d%%) — visual QA complete",
                        avg_similarity, SIMILARITY_TARGET)
            break

        # Check if similarity plateaued (less than 2% improvement)
        if iteration > 0 and avg_similarity - prev_similarity < 2.0:
            logger.info("Similarity plateaued at %.1f%% (was %.1f%%) — stopping",
                        avg_similarity, prev_similarity)
            break

        prev_similarity = avg_similarity

        # Get fixes from Gemini, focusing on desktop first (highest impact)
        iteration_fixes = []
        for vp_name in ["desktop", "tablet", "mobile"]:
            source_ss = source_screenshots.get(vp_name)
            gen_ss = gen_screenshots.get(vp_name)
            if not source_ss or not gen_ss:
                continue

            fixes, gemini_score = await compare_and_score(
                source_ss, gen_ss, comp_def, vp_name,
            )
            if gemini_score > 0:
                logger.info("Gemini estimates %s similarity: %d%%", vp_name, gemini_score)
            iteration_fixes.extend(fixes)

        if not iteration_fixes:
            logger.info("No fixes suggested — visual QA complete")
            break

        # Snapshot comp_def before applying fixes (for rollback)
        import copy
        snapshot = copy.deepcopy(comp_def)

        # Apply fixes to comp_def in place
        applied = _apply_fixes(comp_def, iteration_fixes)
        all_fixes.extend(applied)
        logger.info("Iteration %d: applied %d/%d fixes",
                     iteration + 1, len(applied), len(iteration_fixes))

        if not applied:
            logger.info("No fixes could be applied — stopping")
            break

        # SAVE to API so next screenshot reflects changes
        if save_callback:
            try:
                saved = await save_callback(comp_def)
                if not saved:
                    logger.warning("Save failed after iteration %d — rolling back", iteration + 1)
                    comp_def.clear()
                    comp_def.update(snapshot)
                    break
                logger.info("Saved updated page after iteration %d", iteration + 1)

                # Verify similarity didn't drop — rollback if it did
                verify_screenshots = await take_multi_viewport_screenshots(generated_page_url)
                if verify_screenshots:
                    verify_scores = {}
                    for vp_name in VIEWPORTS:
                        src_ss = source_screenshots.get(vp_name)
                        ver_ss = verify_screenshots.get(vp_name)
                        if src_ss and ver_ss:
                            verify_scores[vp_name] = compute_similarity(src_ss, ver_ss)
                    new_avg = sum(verify_scores.values()) / max(len(verify_scores), 1)

                    if new_avg < avg_similarity - 1.0:
                        logger.warning(
                            "Similarity DROPPED %.1f%% → %.1f%% after fixes — ROLLING BACK",
                            avg_similarity, new_avg,
                        )
                        comp_def.clear()
                        comp_def.update(snapshot)
                        await save_callback(comp_def)
                        # Remove the bad fixes from all_fixes
                        all_fixes = all_fixes[:-len(applied)]
                        break
                    else:
                        logger.info("Post-fix similarity: %.1f%% (was %.1f%%)", new_avg, avg_similarity)

            except Exception as e:
                logger.warning("Save/verify failed: %s — rolling back", e)
                comp_def.clear()
                comp_def.update(snapshot)
                break
        else:
            logger.warning("No save_callback provided — fixes applied to memory only")

    # Final similarity check
    if all_fixes:
        final_screenshots = await take_multi_viewport_screenshots(generated_page_url)
        if final_screenshots:
            final_scores = {}
            for vp_name in VIEWPORTS:
                source_ss = source_screenshots.get(vp_name)
                gen_ss = final_screenshots.get(vp_name)
                if source_ss and gen_ss:
                    final_scores[vp_name] = compute_similarity(source_ss, gen_ss)
            final_avg = sum(final_scores.values()) / max(len(final_scores), 1)
            logger.info("Final similarity: avg=%.1f%% %s",
                        final_avg, {k: f"{v:.1f}%" for k, v in final_scores.items()})

    return all_fixes
