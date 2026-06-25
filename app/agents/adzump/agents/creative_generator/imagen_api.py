"""Gemini Imagen REST API integration service with parallel multi-aspect generation."""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import Any

import httpx

from app.agents.adzump._shared import upload_and_analyze

logger = logging.getLogger(__name__)

# Meta ad placements — 3 aspect ratios generated natively via parallel API calls
ASPECT_CONFIGS: list[tuple[str, str]] = [
    ("square", "1:1"),  # 1080×1080 — feed
    ("portrait", "4:5"),  # 960×1200  — feed / stories
    ("landscape", "16:9"),  # 1200×675  — feed / right column (close to Meta 1.91:1)
]

GEMINI_API_TIMEOUT = 60.0


async def _call_gemini_one(
    parts: list[dict[str, Any]],
    aspect_label: str,
    aspect_ratio: str,
    api_key: str,
) -> tuple[str, str, bytes]:
    """Single Gemini image generation call for one aspect ratio.

    Returns (aspect_label, mime_type, raw_image_bytes).
    """
    payload: dict[str, Any] = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": aspect_ratio},
        },
    }

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-3.1-flash-image-preview:generateContent?key={api_key}"
    )

    async with httpx.AsyncClient(timeout=GEMINI_API_TIMEOUT) as client:
        resp = await client.post(url, json=payload)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Gemini API error for {aspect_label} ({aspect_ratio}): "
                f"status={resp.status_code} body={resp.text[:500]}"
            )

        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            raise RuntimeError(f"No candidates for {aspect_label} ({aspect_ratio})")

        res_parts = candidates[0].get("content", {}).get("parts", [])
        image_part = next((p for p in res_parts if "inlineData" in p), None)
        if not image_part:
            raise RuntimeError(
                f"No image in response for {aspect_label} ({aspect_ratio})"
            )

        raw_bytes = base64.b64decode(image_part["inlineData"].get("data", ""))
        mime = image_part["inlineData"].get("mimeType", "image/jpeg")
        return aspect_label, mime, raw_bytes


async def call_gemini_imagen(
    base_img_b64: str | None,
    mime_base: str | None,
    base_img_path: str | None,
    creative_type_value: str,
    logo_b64: str,
    logo_mime: str,
    ad_copy: dict,
    api_key: str,
    context: dict,
    target_formats: list[str] | None = None,
) -> dict:
    """Generate aspect-ratio variants of a creative in parallel via Gemini Imagen.

    Makes concurrent API calls for each requested format (e.g., 1:1, 4:5, 16:9),
    producing natively-rendered images with text positioned optimally for that ratio.

    Returns a dict with *headline*, *description*, *cta*, *creative_urls*
    (label → CDN URL), *creative_type*, and *base_image_url*.
    """
    headline = ad_copy.get("headline", "")
    cta = ad_copy.get("cta", "")
    scene_description = ad_copy.get(
        "image_prompt",
        "Generate a professional, high-quality ad creative.",
    )

    logger.info(
        "call_gemini_imagen (batch) starting: creative_type=%s, logo_len=%d, base_img_len=%d",
        creative_type_value,
        len(logo_b64),
        len(base_img_b64) if base_img_b64 else 0,
    )

    # Load layout prompt template
    prompts_dir = Path(__file__).resolve().parent / "prompts"
    layout_template = (prompts_dir / "image_layout.txt").read_text(encoding="utf-8")

    # Adapt template if there is no background image to composite
    if not (base_img_b64 and mime_base):
        layout_template = layout_template.replace(
            "compositing the provided brand logo (Image 1) and base background scene (Image 2)",
            "using the provided brand logo (Image 1) and generating a beautiful background scene from scratch",
        )

    prompt = layout_template.format(
        headline=headline,
        description=ad_copy.get("description", ""),
        cta=cta,
        design_composition=ad_copy.get("design_composition", ""),
        color_palette_and_theme=ad_copy.get("color_palette_and_theme", ""),
        scene_description=scene_description,
        rera_no=ad_copy.get("rera_no", "") or "",
        price=ad_copy.get("price", "") or "",
        location=ad_copy.get("location", "") or "",
    )

    logger.info("Batch prompt for Gemini:\n%s", prompt)

    # Build the shared multimodal parts (same for all aspect ratios)
    parts: list[dict[str, Any]] = []
    parts.append({"text": "Image 1 (Brand Logo):\n"})
    parts.append({"inlineData": {"mimeType": logo_mime, "data": logo_b64}})
    if base_img_b64 and mime_base:
        parts.append({"text": "\nImage 2 (Base Background Image):\n"})
        parts.append({"inlineData": {"mimeType": mime_base, "data": base_img_b64}})
    else:
        logger.info("No base background image attached")
    parts.append({"text": f"\nInstructions:\n{prompt}"})

    # Fire parallel Gemini calls for requested aspect ratios
    target_aspects = [cfg for cfg in ASPECT_CONFIGS if cfg[0] == "square"]  # Default/fallback to square
    if target_formats:
        target_aspects = [cfg for cfg in ASPECT_CONFIGS if cfg[0] in target_formats]


    tasks = [
        _call_gemini_one(parts, label, ratio, api_key)
        for label, ratio in target_aspects
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    urls: dict[str, str] = {}
    for r in results:
        if isinstance(r, Exception):
            logger.warning("One aspect-ratio call failed: %s", r)
            continue
        label, mime, raw_bytes = r
        upload_res = await upload_and_analyze(
            raw_bytes,
            mime,
            f"gemini_imagen_{creative_type_value}_{label}",
            "creative",
            context,
        )
        if upload_res and upload_res.get("url"):
            urls[label] = upload_res["url"]
            logger.info("Uploaded %s creative to CDN: %s", label, upload_res["url"])

    if not urls:
        raise RuntimeError("All aspect-ratio Gemini calls failed")

    logger.info(
        "Generated %d/%d sizes for creative_type=%s: %s",
        len(urls),
        len(target_aspects),
        creative_type_value,
        urls,
    )

    return {
        "headline": headline,
        "description": ad_copy.get("description", ""),
        "cta": cta,
        "creative_urls": urls,
        "creative_type": creative_type_value,
        "base_image_url": base_img_path or "",
    }
