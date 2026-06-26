"""Pydantic data models for the Creative Generator sub-agents."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AdCopyOutput(BaseModel):
    """Structured output for generated ad copy."""
    headline: str = Field(description="Headline under 40 characters")
    description: str = Field(description="Description under 80 characters")
    cta: str = Field(description="Call to action text")
    image_prompt: str = Field(description="Detailed prompt for image generation model")
    design_composition: str = Field(description="Detailed art direction blueprint outlining visual structure, framing shapes, and layout arrangements customized to the base image")
    color_palette_and_theme: str = Field(description="Recommended color scheme, harmonies, and tone that complement the base image's colors")
    rera_no: str | None = Field(default=None, description="Verbatim RERA registration/certificate number if applicable")
    price: str | None = Field(default=None, description="Verbatim pricing information if applicable")
    location: str | None = Field(default=None, description="Verbatim location information if applicable")



class ImageSelectionOutput(BaseModel):
    """Structured output for multimodal background image selection."""
    selected_index: int = Field(description="Selected index of the background image (0, 1, or 2), or -1 if all rejected")
    reason: str = Field(description="One-sentence justification for the selection")
    score: int = Field(description="Quality score 1-10 for the selected image")
