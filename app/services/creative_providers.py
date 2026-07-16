from __future__ import annotations

"""
Creative Provider for multi-turn image generation via Gemini Imagen.

Converts between the application's internal message format and the
Gemini API wire format, handling image inline data round-trips.

Internal message format (stored in BaseSession.messages):
    {"role": "user", "content": [
        {"type": "text", "text": "..."},
        {"type": "image_source", "url": "https://cdn/..."}     # lightweight URL ref
    ]}
    {"role": "assistant", "content": [
        {"type": "text", "text": "..."},
        {"type": "image_source", "url": "https://cdn/..."}     # prev generated image
    ]}

Gemini API request format (snake_case):
    {"role": "user", "parts": [
        {"text": "..."},
        {"inline_data": {"mime_type": "...", "data": "<base64>"}}
    ]}

Gemini API response format (camelCase):
    {"candidates": [{"content": {"parts": [
        {"text": "..."},
        {"inlineData": {"mimeType": "...", "data": "<base64>"}}
    ]}}]}
"""

import asyncio
import base64
import logging
from typing import Any, AsyncIterator, Dict, List

import httpx

from app.config import settings
from app.services.llm_provider import LLMProvider, StreamChunk

logger = logging.getLogger(__name__)

GEMINI_IMAGEN_MODEL = "gemini-3.1-flash-image-preview"
GEMINI_API_TIMEOUT = 60.0
GEMINI_MAX_RETRIES = 3
GEMINI_RETRY_DELAY_S = 2.0


class GeminiImagenProvider(LLMProvider):
    """Gemini provider for multi-turn image generation.

    Implements ``stream_completion_with_tools`` (yields ``text_delta``
    and ``image_chunk`` — never ``tool_use``) so the BaseAgent loop can
    drive a conversational image flow with NO tools.
    """

    supports_image_in_tool_result = True

    def __init__(self, api_key: str | None = None) -> None:
        import os

        self._api_key = (
            api_key or os.environ.get("GEMINI_API_KEY") or settings.GOOGLE_API_KEY
        )

    # ── LLMProvider interface ──────────────────────────────────────

    @property
    def name(self) -> str:
        return "gemini_imagen"

    def get_model(self, tier: str) -> str:
        return GEMINI_IMAGEN_MODEL

    def supports_vision(self) -> bool:
        return True

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
        raise NotImplementedError(
            "GeminiImagenProvider does not support create_completion. "
            "Use stream_completion_with_tools for image chat."
        )

    async def create_completion_with_tools(
        self,
        system_prompt: Any,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        model_tier: str = "balanced",
        max_tokens: int = 16384,
    ) -> Dict[str, Any]:
        raise NotImplementedError(
            "GeminiImagenProvider does not support create_completion_with_tools."
        )

    async def stream_completion_with_tools(
        self,
        system_prompt: Any,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]] | None = None,
        model_tier: str = "balanced",
        max_tokens: int = 16384,
        context_management: dict | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a multi-turn image conversation via Gemini Imagen.

        Yields ``text_delta`` for text parts and ``image_chunk`` for
        generated image parts. Never yields ``tool_use`` blocks — this
        provider is for pure conversational image generation.
        """
        aspect_ratio = self._resolve_aspect_from_messages(messages, context_management)

        contents = await self._convert_messages(system_prompt, messages)

        payload = {
            "contents": contents,
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
            },
        }
        if aspect_ratio != "1:1":
            payload["generationConfig"]["imageConfig"] = {"aspectRatio": aspect_ratio}

        import copy

        log_payload = copy.deepcopy(payload)
        for content in log_payload.get("contents", []):
            for part in content.get("parts", []):
                if "inline_data" in part and "data" in part["inline_data"]:
                    b64_len = len(part["inline_data"]["data"])
                    part["inline_data"]["data"] = f"<base64 data: {b64_len} chars>"
        logger.info("Gemini Request Payload: %s", log_payload)

        data = await self._post_gemini(payload)

        logger.info(
            "Gemini response keys=%s, candidates=%s",
            list(data.keys()),
            len(data.get("candidates") or []),
        )
        if data.get("candidates"):
            c0 = data["candidates"][0]
            finish = c0.get("finishReason", "?")
            parts = c0.get("content", {}).get("parts", [])
            part_types = [list(p.keys()) for p in parts]
            logger.info(
                "Gemini candidate 0: finish=%s parts=%s text_preview=%s",
                finish,
                part_types,
                (
                    parts[0].get("text", "")[:200]
                    if parts and "text" in parts[0]
                    else "(none)"
                ),
            )

        candidate = data.get("candidates", [None])[0]
        if not candidate:
            yield StreamChunk(type="text_delta", text="[Gemini returned no response.]")
            yield StreamChunk(
                type="done",
                stop_reason="end_turn",
                usage={"input_tokens": 0, "output_tokens": 0},
            )
            return

        res_parts = candidate.get("content", {}).get("parts", [])

        for part in res_parts:
            if "text" in part:
                yield StreamChunk(type="text_delta", text=part["text"])
            elif "inlineData" in part:
                raw = base64.b64decode(part["inlineData"]["data"])
                mime = part["inlineData"].get("mimeType", "image/png")
                logger.info(
                    "Gemini returned image: mime=%s size=%d bytes",
                    mime,
                    len(raw),
                )
                yield StreamChunk(type="image_chunk", image_data=raw, image_mime=mime)

        usage = data.get("usageMetadata", {})
        yield StreamChunk(
            type="done",
            stop_reason="end_turn",
            usage={
                "input_tokens": usage.get("promptTokenCount", 0),
                "output_tokens": usage.get("candidatesTokenCount", 0),
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        )

    # ── Internal: message conversion ──────────────────────────────

    @staticmethod
    def _extract_system_text(system_prompt: Any) -> str:
        """Extract plain text from a system prompt (string or list of blocks)."""
        if isinstance(system_prompt, list):
            return " ".join(
                block.get("text", "")
                for block in system_prompt
                if isinstance(block, dict) and block.get("type") == "text"
            )
        if isinstance(system_prompt, str):
            return system_prompt
        return ""

    async def _convert_messages(
        self,
        system_prompt: Any,
        messages: List[Dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Convert app-format messages to Gemini ``contents`` array.

        - ``image_source`` blocks are downloaded from CDN and inlined
          as ``inline_data`` (snake_case per Gemini API spec).
        - Previous model responses with images are fully preserved in
          the history so Gemini sees the full multi-turn context.
        - ``assistant`` role is mapped to ``model`` for Gemini API.
        """
        contents: list[dict[str, Any]] = []

        sys_text = self._extract_system_text(system_prompt)

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content")

            if role == "assistant":
                role = "model"

            if isinstance(content, str):
                parts = [{"text": content}]
                contents.append({"role": role, "parts": parts})
                continue

            if not isinstance(content, list):
                continue

            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    parts.append({"text": block["text"]})
                elif btype == "image_source":
                    url = block.get("url", "")
                    if url:
                        try:
                            b64, mime = await self._download_and_encode(url)
                            parts.append(
                                {
                                    "inline_data": {
                                        "mime_type": mime,
                                        "data": b64,
                                    }
                                }
                            )
                        except Exception as e:
                            logger.warning(
                                "Failed to download image_source %s: %s", url, e
                            )

            if parts:
                contents.append({"role": role, "parts": parts})

        if sys_text:
            contents.insert(
                0,
                {
                    "role": "user",
                    "parts": [{"text": f"<system>\n{sys_text}\n</system>"}],
                },
            )

        return contents

    # ── Internal: network helpers ──────────────────────────────────

    async def _download_and_encode(self, url: str) -> tuple[str, str]:
        """Download an image URL and return ``(base64_data, mime_type)``.

        Relative URLs are resolved against ``GATEWAY_URL``.
        """
        if url.startswith("/"):
            gateway = settings.GATEWAY_URL.rstrip("/")
            url = f"{gateway}{url}"
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = base64.b64encode(resp.content).decode("utf-8")
            ctype = resp.headers.get("content-type", "image/jpeg")
            return data, ctype

    def _build_url(self) -> str:
        return (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_IMAGEN_MODEL}:generateContent?key={self._api_key}"
        )

    async def _post_gemini(self, payload: dict) -> dict[str, Any]:
        """POST to Gemini with retry logic."""
        url = self._build_url()
        last_exc: Exception | None = None
        for attempt in range(GEMINI_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=GEMINI_API_TIMEOUT) as client:
                    resp = await client.post(url, json=payload)
                    if resp.status_code != 200:
                        raise RuntimeError(
                            f"Gemini API error: status={resp.status_code} "
                            f"body={resp.text[:500]}"
                        )
                    return resp.json()
            except httpx.ReadTimeout as e:
                last_exc = e
                logger.warning(
                    "Gemini timeout attempt %d/%d", attempt + 1, GEMINI_MAX_RETRIES
                )
                if attempt < GEMINI_MAX_RETRIES - 1:
                    await asyncio.sleep(GEMINI_RETRY_DELAY_S)
            except Exception as e:
                last_exc = e
                raise RuntimeError(f"Gemini API call failed: {e}") from e
        raise RuntimeError(
            f"Gemini API timed out after {GEMINI_MAX_RETRIES} attempts."
        ) from last_exc

    # ── Internal: aspect ratio ─────────────────────────────────────

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

    def _resolve_aspect_from_messages(
        self, messages: list, context_management: dict | None = None
    ) -> str:
        """Extract aspect ratio from context_management (set by ImageAgent).

        Falls back to the last generated image's dimensions in the message
        history for multi-turn editing (preserves the original aspect ratio
        across edit rounds).
        """
        if context_management and "aspect_ratio" in context_management:
            return context_management["aspect_ratio"]
        return "1:1"


_provider: GeminiImagenProvider | None = None


def get_creative_provider() -> GeminiImagenProvider:
    global _provider
    if _provider is None:
        _provider = GeminiImagenProvider()
        logger.info("GeminiImagenProvider created (model=%s)", GEMINI_IMAGEN_MODEL)
    return _provider
