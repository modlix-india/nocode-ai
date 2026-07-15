from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ImageResult:
    image: bytes
    mime_type: str
    prompt: str


@dataclass
class ImageBrief:
    creative_id: str
    prompt: str
    text: str
    width: int
    height: int
    aspect_ratio: str
    mime_type: str = "image/jpeg"


@dataclass
class Creative:
    id: str
    status: str
    format_label: str
    width: int
    height: int
    prompt: str
    prompt_history: list[str] = field(default_factory=list)
    image_url: str | None = None
    headline: str | None = None
    description: str | None = None
    cta: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "format_label": self.format_label,
            "width": self.width,
            "height": self.height,
            "prompt": self.prompt,
            "prompt_history": self.prompt_history,
            "image_url": self.image_url,
            "headline": self.headline,
            "description": self.description,
            "cta": self.cta,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict | Creative) -> Creative:
        if isinstance(data, cls):
            return data
        return cls(
            id=data["id"],
            status=data["status"],
            format_label=data["format_label"],
            width=int(data["width"]),
            height=int(data["height"]),
            prompt=data["prompt"],
            prompt_history=data.get("prompt_history") or [],
            image_url=data.get("image_url"),
            headline=data.get("headline"),
            description=data.get("description"),
            cta=data.get("cta"),
            error=data.get("error"),
        )
