"""creative_generation — tools for generating ad copy and image creatives using Gemini."""

from __future__ import annotations

import logging

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.agents.creative_generator.agent import (
    get_creative_generator_agent,
)

logger = logging.getLogger(__name__)


async def _generate_fresh_creatives(params: dict, context: dict) -> ToolResult:
    """Generate fresh ad copy and square creatives from scratch."""
    return await get_creative_generator_agent().generate(params, context)


async def _modify_existing_creative(params: dict, context: dict) -> ToolResult:
    """Modify or regenerate aspect ratios of an existing ad creative."""
    return await get_creative_generator_agent().modify(params, context)


generate_fresh_creatives = ToolDefinition(
    name="generate_fresh_creatives",
    description=(
        "Generate a fresh set of ad copies and square ad creatives from scratch. "
        "Downloads base background images, selects candidates, and generates a premium square image."
    ),
    display_name="Generate Fresh Creatives",
    parameters=[
        ToolParameter(
            name="custom_theme",
            type="string",
            description="Optional visual theme override (e.g. 'sunset background').",
            required=False,
        ),
        ToolParameter(
            name="target_personas",
            type="string",
            description="Optional comma-separated list of target demographics/personas (e.g. 'elite, families, students').",
            required=False,
        ),
    ],
    execute=_generate_fresh_creatives,
)


modify_existing_creative = ToolDefinition(
    name="modify_existing_creative",
    description=(
        "Modify, update, or generate alternative aspect ratios (portrait, landscape) "
        "for a specific previously generated ad creative."
    ),
    display_name="Modify Existing Creative",
    parameters=[
        ToolParameter(
            name="target_creative_index",
            type="integer",
            description="Required 1-based index of the specific creative to regenerate/update.",
            required=True,
        ),
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
        ToolParameter(
            name="target_formats",
            type="string",
            description="Optional comma-separated list of formats to generate (e.g. 'square,portrait,landscape'). If omitted, defaults to the formats already present.",
            required=False,
        ),
        ToolParameter(
            name="custom_background_image",
            type="string",
            description="Optional CDN URL or file path of a custom background image to use.",
            required=False,
        ),
        ToolParameter(
            name="edited_creative_url",
            type="string",
            description="Optional URL of the generated ad creative being edited for layout reference.",
            required=False,
        ),
    ],
    execute=_modify_existing_creative,
)


CREATIVE_GENERATION_TOOLS = [generate_fresh_creatives, modify_existing_creative]
