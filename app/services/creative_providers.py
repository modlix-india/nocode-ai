from __future__ import annotations

"""
Creative Provider abstraction for AI image generation.
Subclasses LLMProvider to share the same base provider identity,
while raising NotImplementedError for standard text completion and exposing
specialized image generate/edit methods.

Supports:
- Gemini Imagen (via Gemini REST API)

Usage:
    from app.services.creative_providers import get_creative_provider
    provider = get_creative_provider()
    result = await provider.generate("a cat", 1080, 1080)
"""

import base64
import logging
from dataclasses import dataclass
from typing import Any, Dict, List

import httpx

from app.config import settings
from app.services.llm_provider import LLMProvider

logger = logging.getLogger(__name__)

GEMINI_IMAGEN_MODEL = "gemini-3.1-flash-image-preview"
GEMINI_API_TIMEOUT = 60.0


@dataclass
class GenerationResult:
    image: bytes
    mime_type: str
    prompt: str


class GeminiImagenProvider(LLMProvider):
    def __init__(self, api_key: str | None = None) -> None:
        import os

        self._api_key = (
            api_key or os.environ.get("GEMINI_API_KEY") or settings.GOOGLE_API_KEY
        )

    @property
    def name(self) -> str:
        return "gemini_imagen"

    def get_model(self, tier: str) -> str:
        return GEMINI_IMAGEN_MODEL

    def supports_vision(self) -> bool:
        return False

    def supports_prompt_caching(self) -> bool:
        return False

    async def create_completion(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        model_tier: str = "balanced",
        max_tokens: int = 8192,
        use_cache: bool = True,
    ) -> Dict[str, Any]:
        """Not supported for Image Provider. Standard text completion is kept idle."""
        raise NotImplementedError(
            "GeminiImagenProvider does not support create_completion. "
            "Use generate() or edit() instead."
        )

    async def create_completion_with_tools(
        self,
        system_prompt: Any,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        model_tier: str = "balanced",
        max_tokens: int = 16384,
    ) -> Dict[str, Any]:
        """Not supported for Image Provider."""
        raise NotImplementedError(
            "GeminiImagenProvider does not support create_completion_with_tools."
        )

    def _build_url(self) -> str:
        return (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_IMAGEN_MODEL}:generateContent?key={self._api_key}"
        )

    def _resolve_aspect(self, width: int, height: int) -> str:
        ratio = width / height
        if abs(ratio - 1.0) < 0.05:
            return "1:1"
        if abs(ratio - 4 / 5) < 0.05:
            return "4:5"
        if abs(ratio - 16 / 9) < 0.05:
            return "16:9"
        if abs(ratio - 9 / 16) < 0.05:
            return "9:16"
        if abs(ratio - 1.91) < 0.05:
            return "1.91:1"
        return f"{width}:{height}"

    async def generate(
        self,
        prompt: str,
        width: int = 1080,
        height: int = 1080,
        aspect_ratio: str | None = None,
        logo_bytes: bytes | None = None,
        logo_mime: str | None = None,
        base_image_bytes: bytes | None = None,
        base_image_mime: str | None = None,
    ) -> GenerationResult:
        ar = aspect_ratio or self._resolve_aspect(width, height)
        parts: list[dict[str, Any]] = []
        if logo_bytes and logo_mime:
            b64_logo = base64.b64encode(logo_bytes).decode("utf-8")
            parts.append({"text": "Image 1 (Brand Logo):\n"})
            parts.append({"inlineData": {"mimeType": logo_mime, "data": b64_logo}})
        if base_image_bytes and base_image_mime:
            b64_base = base64.b64encode(base_image_bytes).decode("utf-8")
            parts.append({"text": "\nImage 2 (Base Background Image):\n"})
            parts.append(
                {"inlineData": {"mimeType": base_image_mime, "data": b64_base}}
            )
        parts.append({"text": f"\nInstructions:\n{prompt}"})

        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {"aspectRatio": ar},
            },
        }
        url = self._build_url()
        async with httpx.AsyncClient(timeout=GEMINI_API_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Gemini Imagen generate failed: status={resp.status_code} "
                    f"body={resp.text[:500]}"
                )
            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                raise RuntimeError("Gemini Imagen: no candidates in response")
            res_parts = candidates[0].get("content", {}).get("parts", [])
            image_part = next((p for p in res_parts if "inlineData" in p), None)
            if not image_part:
                raise RuntimeError("Gemini Imagen: no image data in response")
            raw = base64.b64decode(image_part["inlineData"]["data"])
            mime = image_part["inlineData"].get("mimeType", "image/jpeg")
            return GenerationResult(image=raw, mime_type=mime, prompt=prompt)

    async def edit(
        self,
        messages: List[Dict[str, Any]],
        width: int = 1080,
        height: int = 1080,
        aspect_ratio: str | None = None,
    ) -> GenerationResult:
        ar = aspect_ratio or self._resolve_aspect(width, height)
        contents = []
        for msg in messages:
            role = msg.get("role")
            parts = []
            if role == "model" and "image_data" in msg:
                parts.append(
                    {
                        "inlineData": {
                            "mimeType": msg.get("mime_type", "image/jpeg"),
                            "data": msg["image_data"],
                        }
                    }
                )
            else:
                parts.append({"text": msg.get("content", "")})
            contents.append({"role": role, "parts": parts})

        payload = {
            "contents": contents,
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {"aspectRatio": ar},
            },
        }
        url = self._build_url()
        async with httpx.AsyncClient(timeout=GEMINI_API_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Gemini Imagen edit failed: status={resp.status_code} "
                    f"body={resp.text[:500]}"
                )
            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                raise RuntimeError("Gemini Imagen: no candidates in edit response")
            res_parts = candidates[0].get("content", {}).get("parts", [])
            image_part = next((p for p in res_parts if "inlineData" in p), None)
            if not image_part:
                raise RuntimeError("Gemini Imagen: no image data in edit response")
            raw = base64.b64decode(image_part["inlineData"]["data"])
            mime = image_part["inlineData"].get("mimeType", "image/jpeg")
            last_prompt = next(
                (
                    m.get("content", "")
                    for m in reversed(messages)
                    if m.get("role") == "user"
                ),
                "",
            )
            return GenerationResult(image=raw, mime_type=mime, prompt=last_prompt)


_provider: GeminiImagenProvider | None = None


def get_creative_provider() -> GeminiImagenProvider:
    global _provider
    if _provider is None:
        _provider = GeminiImagenProvider()
        logger.info("GeminiImagenProvider created (model=%s)", GEMINI_IMAGEN_MODEL)
    return _provider
