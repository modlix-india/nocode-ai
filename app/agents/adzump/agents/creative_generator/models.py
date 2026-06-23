"""Pydantic data models for the Creative Generator sub-agents."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AdCopyOutput(BaseModel):
    """Structured output for generated ad copy."""
    headline: str = Field(description="Headline under 40 characters")
    description: str = Field(description="Description under 80 characters")
    cta: str = Field(description="Call to action text")
    image_prompt: str = Field(description="Detailed prompt for image generation model")
    rera_no: str | None = Field(default=None, description="Verbatim RERA registration/certificate number if applicable")
    price: str | None = Field(default=None, description="Verbatim pricing information if applicable")
    location: str | None = Field(default=None, description="Verbatim location information if applicable")


class ImageSelectionOutput(BaseModel):
    """Structured output for multimodal background image selection."""
    selected_index: int = Field(description="Selected index of the background image (0, 1, or 2)")
    reasoning: str = Field(description="Explanation of why this candidate was selected")
