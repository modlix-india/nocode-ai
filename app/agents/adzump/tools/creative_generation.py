"""creative_generation — tools for generating ad copy and image creatives using Gemini."""

from __future__ import annotations

import logging

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.agents.creative_generator.generator import (
    generate_ad_copy_and_prompt_impl,
)

logger = logging.getLogger(__name__)


async def _generate_ad_copy_and_prompt(params: dict, context: dict) -> ToolResult:
    """Generate ad copy and images in a single call."""
    return await generate_ad_copy_and_prompt_impl(params, context)


generate_ad_copy_and_prompt = ToolDefinition(
    name="generate_ad_copy_and_prompt",
    description=(
        "Generate ad copy (Headline, Description, CTA) and a detailed Imagen 3 prompt, "
        "then immediately generate the final ad creatives using Gemini Imagen REST API. "
        "Downloads base images, selects best candidates, and calls the generation model."
    ),
    display_name="Generate Ad Creatives",
    parameters=[
        ToolParameter(
            name="custom_headline",
            type="string",
            description="Optional custom override for ad headline.",
            required=False,
        ),
        ToolParameter(
            name="custom_description",
            type="string",
            description="Optional custom override for ad description.",
            required=False,
        ),
        ToolParameter(
            name="custom_cta",
            type="string",
            description="Optional custom override for ad call-to-action.",
            required=False,
        ),
        ToolParameter(
            name="custom_theme",
            type="string",
            description="Optional visual theme override (e.g. 'sunset background').",
            required=False,
        ),
    ],
    execute=_generate_ad_copy_and_prompt,
)

CREATIVE_GENERATION_TOOLS = [generate_ad_copy_and_prompt]
