"""Screenshot style analyzer — uses Claude Haiku vision to extract design systems.

Takes a screenshot (base64 PNG) of a website and analyzes it to extract:
- Color palette (primary, secondary, accent, background, text colors with hex values)
- Typography (font families, sizes, weights for headings/body)
- Layout patterns (section types, grid structure, spacing)
- Component styles (navbar, hero, cards, buttons, footer patterns)

This bridges the gap between what the HTML scraper captures (structure + inline styles)
and the actual visual design (which may be in external CSS we can't fetch).

Uses Claude Haiku 4.5 for cheap vision analysis (~$0.25/1M input tokens).
One call per URL, runs during the auto-scrape phase.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_STYLE_ANALYSIS_PROMPT = """\
Analyze this website screenshot and extract the complete design system. Be EXTREMELY specific with values.

Return a structured analysis with these sections:

## Colors (extract exact hex values from what you see)
- Primary color (main brand color): #hex
- Secondary color: #hex
- Accent color (CTAs, highlights): #hex
- Background colors (body, sections, cards): list each with #hex
- Text colors (headings, body, muted): #hex each
- Gradient if any: describe direction and colors

## Typography
- Heading font: name, approximate sizes (h1, h2, h3 in px)
- Body font: name, size, line-height
- Font weights used (light, regular, medium, bold)

## Layout (describe each visible section top to bottom)
For each section describe:
- Section type (navbar, hero, features, about, gallery, contact, footer, etc.)
- Background (solid color, gradient, image with overlay?)
- Layout (full-width, contained, grid columns, flex direction)
- Approximate height/padding
- Key visual elements (background images, overlays, shadows, borders, rounded corners)

## Component Patterns
- Navbar: background color, height, logo position, link styles, CTA button style
- Hero: background treatment (image? overlay color+opacity?), text alignment, heading size
- Cards: background, border-radius, shadow, padding, image treatment
- Buttons: background color, text color, border-radius, padding, hover effect
- Footer: background color, text color, layout

## Responsive Notes
- Is the layout responsive? Approximate breakpoint behavior if visible.

Be as specific as possible with pixel values and hex colors. This data will be used to recreate the site programmatically.
"""


async def analyze_screenshot_styles(screenshot_base64: str, url: str = "") -> str:
    """Analyze a website screenshot using Claude Haiku vision.

    Args:
        screenshot_base64: Base64-encoded PNG screenshot.
        url: The source URL (for context in the prompt).

    Returns:
        Structured design analysis text, or error message.
    """
    try:
        import google.generativeai as genai
        from app.config import settings

        if not settings.GOOGLE_API_KEY:
            logger.info("GOOGLE_API_KEY not set — skipping vision style analysis")
            return ""

        genai.configure(api_key=settings.GOOGLE_API_KEY)
        model = genai.GenerativeModel("gemini-2.0-flash")

        import base64
        image_bytes = base64.b64decode(screenshot_base64)

        context = f"This is a screenshot of {url}. " if url else ""

        response = await asyncio.to_thread(
            model.generate_content,
            [
                {"mime_type": "image/png", "data": image_bytes},
                context + _STYLE_ANALYSIS_PROMPT,
            ],
        )

        result = response.text if response.text else ""
        logger.info("Screenshot style analysis complete for %s: %d chars", url, len(result))
        return result

    except ImportError:
        logger.warning("google-generativeai package not available for style analysis")
        return ""
    except Exception as e:
        logger.warning("Screenshot style analysis failed for %s: %s", url, e)
        return ""


async def analyze_and_format_styles(
    screenshot_base64: str,
    scraped_data: dict[str, Any],
) -> str:
    """Run vision analysis AND include pre-converted Modlix styleProperties.

    Combines:
    1. Gemini Flash vision analysis (colors, fonts, layout description)
    2. Pre-converted Modlix styleProperties per section (ready to use)
    3. Scraped navigation, images, and text content
    """
    from app.agents.appbuilder.tools.web_scraper import format_scraped_data_with_styles

    url = scraped_data.get("url", "")

    # Run vision analysis
    style_analysis = await analyze_screenshot_styles(screenshot_base64, url)

    # Get pre-converted styles data
    styles_data = format_scraped_data_with_styles(scraped_data)

    parts: list[str] = []

    # Vision analysis first (design system overview)
    if style_analysis:
        parts.append(f"## Visual Design Analysis (from screenshot of {url})")
        parts.append(style_analysis)

    # Then pre-converted styles (ready-to-use Modlix format)
    parts.append(styles_data)

    return "\n\n".join(parts)
