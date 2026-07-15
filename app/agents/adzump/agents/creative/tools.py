from __future__ import annotations

import logging
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.agents.creative.image_agent import get_image_agent
from app.agents.adzump.agents.creative.models import Creative, ImageBrief
from app.agents.adzump._shared import emit_progress

logger = logging.getLogger(__name__)


async def _create_creative(params: dict, context: dict) -> ToolResult:
    prompt = (params.get("prompt") or "").strip()
    if not prompt:
        return ToolResult(success=False, error="prompt is required.")
    try:
        width = int(params.get("width", 1080))
        height = int(params.get("height", 1080))
    except (TypeError, ValueError):
        return ToolResult(success=False, error="width and height must be integers.")

    session_ctx = context.get("session_context") or {}
    auth = context.get("auth")
    stream = context.get("event_stream")

    await emit_progress(
        context, "Preparing ad creative copy (headline, description, CTA)..."
    )

    from app.agents.adzump.agents.creative.agent import ensure_creatives_hydrated

    creatives = ensure_creatives_hydrated(session_ctx)
    creative_id = f"cr_{len(creatives) + 1}"

    # Download brand assets if available
    logo_bytes, logo_mime = None, None
    pdata = session_ctx.get("product_data") or context.get("product_data") or {}
    logos = pdata.get("assets", {}).get("logos") or []
    logo_url = (
        session_ctx.get("logo_url")
        or context.get("logo_url")
        or (logos[0].get("url") if logos else None)
    )
    if logo_url:
        await emit_progress(context, "Downloading brand logo...")
        try:
            logo_bytes, logo_mime = await _download_image(logo_url, as_png=True)
        except Exception as e:
            logger.warning("Failed to download logo from %s: %s", logo_url, e)

    base_bytes, base_mime = None, None
    base_image_url = session_ctx.get("base_image_url") or context.get("base_image_url")

    if not base_image_url:
        scraped_images = pdata.get("assets", {}).get("images") or []
        image_urls = [img.get("url") for img in scraped_images if img.get("url")]
        pool = (
            session_ctx.get("creative_images")
            or pdata.get("creative_images")
            or context.get("creative_images")
            or image_urls
        )
        if pool:
            from app.agents.adzump.agents.creative.selection import select_best_image

            btype = (
                session_ctx.get("business_type")
                or context.get("business_type")
                or "business"
            )
            provider = (
                session_ctx.get("provider") or context.get("provider") or "openai"
            )
            selected_url = await select_best_image(
                pool=pool,
                business_type=btype,
                context=context,
                provider_name=provider,
            )
            if selected_url:
                base_image_url = selected_url
                session_ctx["base_image_url"] = selected_url

    if base_image_url:
        await emit_progress(
            context, "Selecting and downloading base background image..."
        )
        try:
            base_bytes, base_mime = await _download_image(base_image_url)
        except Exception as e:
            logger.warning(
                "Failed to download base background image from %s: %s",
                base_image_url,
                e,
            )

    await emit_progress(
        context, "Formatting ad composition template with copywriting details..."
    )

    from pathlib import Path

    prompts_dir = Path(__file__).resolve().parent / "prompts"
    layout_template = (prompts_dir / "image_layout.txt").read_text(encoding="utf-8")

    if not base_bytes:
        layout_template = layout_template.replace(
            "compositing the provided brand logo (Image 1) and base background scene (Image 2)",
            "using the provided brand logo (Image 1) and generating a beautiful background scene from scratch",
        )

    allowed_fallbacks = (
        "   - A highly stylized premium interior scene or room suited for the business vertical.\n"
        "   - A clean modern outdoor view or lifestyle backdrop suited for the business vertical.\n"
        "   - An abstract graphic design with premium gradient backgrounds and geometric design lines."
    )

    copy_lines = []
    if params.get("headline"):
        copy_lines.append(f"- Headline: \"{params['headline']}\"")
    if params.get("cta"):
        copy_lines.append(f"- CTA Button: \"{params['cta']}\"")
    if params.get("price") and params["price"].strip().lower() != "price on request":
        copy_lines.append(f"- \"{params['price']}\" (Render this exact text, do NOT add 'Price:')")
    if params.get("location"):
        copy_lines.append(f"- \"{params['location']}\" (Render this exact text, do NOT add 'Location:')")
    if params.get("rera_no"):
        copy_lines.append(f"- \"{params['rera_no']}\" (Render as a tiny, low-opacity footnote at the bottom, do NOT add 'RERA ID:')")
        
    ad_copy_block = "\n".join(copy_lines)

    final_prompt = layout_template.format(
        ad_copy_block=ad_copy_block,
        design_composition=params.get("design_composition")
        or "Clean modern visual composition.",
        color_palette_and_theme=params.get("color_palette_and_theme")
        or "Premium brand color harmony.",
        scene_description=prompt,
        allowed_fallbacks=allowed_fallbacks,
    )

    brief = ImageBrief(
        creative_id=creative_id,
        prompt=final_prompt,
        text=prompt,
        width=width,
        height=height,
        aspect_ratio=_resolve_aspect(width, height),
    )

    creative = Creative(
        id=creative_id,
        status="generating",
        format_label=_format_label(width, height),
        width=width,
        height=height,
        prompt=prompt,
        prompt_history=[prompt],
    )
    creatives.append(creative)

    await emit_progress(context, "Generating ad creative via Gemini Imagen...")

    try:
        agent = get_image_agent()
        result = await agent.generate(
            brief=brief,
            auth=auth,
            event_stream=stream,
            logo_bytes=logo_bytes,
            logo_mime=logo_mime,
            base_image_bytes=base_bytes,
            base_image_mime=base_mime,
        )
    except Exception as e:
        creative.status = "error"
        creative.error = str(e)
        return ToolResult(
            success=False,
            error=f"Image generation failed: {e}",
        )

    await emit_progress(context, "Uploading generated creative to CDN...")
    logger.info(
        "create_creative uploading image of length=%d type=%s",
        len(result.image),
        result.mime_type,
    )

    image_url = await _upload_image(
        result.image,
        result.mime_type,
        creative_id,
        "creative",
        context,
    )
    logger.info("create_creative uploaded image CDN URL: %s", image_url)

    creative.status = "done"
    creative.image_url = image_url or None
    creative.headline = params.get("headline")
    creative.description = params.get("description")
    creative.cta = params.get("cta")

    summary = f"Created {_format_label(width, height)} creative"
    if creative.image_url:
        summary += f"\nimage_url={creative.image_url}"
    logger.info(
        "create_creative tool finished. summary=%r, image_url=%s",
        summary,
        creative.image_url,
    )

    return ToolResult(
        success=True,
        data={"creative": creative.to_dict()},
        summary=summary,
    )


async def _edit_creative(params: dict, context: dict) -> ToolResult:
    creative_id = (params.get("creative_id") or "").strip()
    changes = (params.get("changes") or "").strip()
    if not creative_id or not changes:
        return ToolResult(
            success=False,
            error="creative_id and changes are required.",
        )

    session_ctx = context.get("session_context") or {}
    from app.agents.adzump.agents.creative.agent import ensure_creatives_hydrated

    creatives = ensure_creatives_hydrated(session_ctx)
    target = _find_creative(creatives, creative_id)
    if not target:
        return ToolResult(
            success=False,
            error=f"Creative '{creative_id}' not found.",
        )

    if not target.image_url:
        return ToolResult(
            success=False,
            error=f"Creative '{creative_id}' has no image to edit.",
        )

    auth = context.get("auth")
    stream = context.get("event_stream")

    await emit_progress(context, "Downloading existing creative image for editing...")

    try:
        image_bytes, mime_type = await _download_image(target.image_url)
    except Exception as e:
        return ToolResult(
            success=False,
            error=f"Failed to download creative '{creative_id}': {e}",
        )

    import base64

    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    messages = [
        {"role": "user", "content": target.prompt_history[0]},
        {"role": "model", "image_data": b64_image, "mime_type": mime_type},
        {
            "role": "user",
            "content": f"Update this image based on this request: {changes}. Do not change any other elements of the image.",
        },
    ]

    edit_prompt = f"{target.prompt} — {changes}"
    brief = ImageBrief(
        creative_id=creative_id,
        prompt=edit_prompt,
        text=f"Original: {target.prompt}\nEdit: {changes}",
        width=target.width,
        height=target.height,
        aspect_ratio=_resolve_aspect(target.width, target.height),
    )

    await emit_progress(context, "Applying edits to creative via Gemini Imagen...")

    try:
        agent = get_image_agent()
        result = await agent.edit(
            brief=brief,
            messages=messages,
            auth=auth,
            event_stream=stream,
        )
    except Exception as e:
        return ToolResult(
            success=False,
            error=f"Image edit failed: {e}",
        )

    await emit_progress(context, "Uploading edited creative to CDN...")

    image_url = await _upload_image(
        result.image,
        result.mime_type,
        f"{creative_id}_edit",
        "creative",
        context,
    )

    target.prompt = edit_prompt
    target.prompt_history.append(changes)
    target.image_url = image_url or None
    target.status = "done"

    summary = f"Edited {target.format_label} ({creative_id}): {changes}"
    if target.image_url:
        summary += f"\nimage_url={target.image_url}"

    return ToolResult(
        success=True,
        data={"creative": target.to_dict()},
        summary=summary,
    )


async def _list_creatives(params: dict, context: dict) -> ToolResult:
    session_ctx = context.get("session_context") or {}
    from app.agents.adzump.agents.creative.agent import ensure_creatives_hydrated

    creatives = ensure_creatives_hydrated(session_ctx)
    if not creatives:
        return ToolResult(
            success=True,
            data={"creatives": []},
            summary="No creatives generated yet.",
        )
    return ToolResult(
        success=True,
        data={"creatives": [c.to_dict() for c in creatives]},
        summary=f"{len(creatives)} creative(s) generated.",
    )


create_creative = ToolDefinition(
    name="create_creative",
    description="Generate a new ad creative image. Provide the visual prompt, dimensions, and optional copy.",
    display_name="Create Creative",
    parameters=[
        ToolParameter(
            name="prompt",
            type="string",
            description="Detailed visual prompt for the image.",
            required=True,
        ),
        ToolParameter(
            name="width",
            type="integer",
            description="Image width in pixels (default 1080).",
            required=False,
        ),
        ToolParameter(
            name="height",
            type="integer",
            description="Image height in pixels (default 1080).",
            required=False,
        ),
        ToolParameter(
            name="headline",
            type="string",
            description="Optional ad headline text.",
            required=False,
        ),
        ToolParameter(
            name="description",
            type="string",
            description="Optional ad description text.",
            required=False,
        ),
        ToolParameter(
            name="cta",
            type="string",
            description="Optional call-to-action text.",
            required=False,
        ),
        ToolParameter(
            name="price",
            type="string",
            description="Optional price text (e.g. 'Starting at $500k').",
            required=False,
        ),
        ToolParameter(
            name="location",
            type="string",
            description="Optional location text (e.g. 'Downtown Manhattan').",
            required=False,
        ),
        ToolParameter(
            name="rera_no",
            type="string",
            description="Optional RERA registration number for real estate compliance.",
            required=False,
        ),
    ],
    execute=_create_creative,
)

edit_creative = ToolDefinition(
    name="edit_creative",
    description="Edit an existing creative image. Specify the creative ID and what to change.",
    display_name="Edit Creative",
    parameters=[
        ToolParameter(
            name="creative_id",
            type="string",
            description="ID or 1-based index of the creative to edit.",
            required=True,
        ),
        ToolParameter(
            name="changes",
            type="string",
            description="Description of changes to make (e.g. 'make it brighter, warmer tones').",
            required=True,
        ),
    ],
    execute=_edit_creative,
)

list_creatives = ToolDefinition(
    name="list_creatives",
    description="List all creatives generated so far in this session.",
    display_name="List Creatives",
    parameters=[],
    execute=_list_creatives,
)

CREATIVE_TOOLS = [create_creative, edit_creative, list_creatives]


def _resolve_aspect(width: int, height: int) -> str:
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


def _format_label(width: int, height: int) -> str:
    ar = _resolve_aspect(width, height)
    names = {
        "1:1": "square",
        "4:5": "portrait",
        "16:9": "landscape",
        "9:16": "story",
        "1.91:1": "social",
    }
    name = names.get(ar, "custom")
    return f"{name} {width}x{height}"


def _find_creative(creatives: list[Creative], creative_id: str) -> Creative | None:
    for i, c in enumerate(creatives):
        if c.id == creative_id:
            return c
    try:
        idx = int(creative_id) - 1
        if 0 <= idx < len(creatives):
            return creatives[idx]
    except (ValueError, TypeError):
        pass
    return None


async def _upload_image(
    image_bytes: bytes,
    mime_type: str,
    source_name: str,
    kind: str,
    context: dict,
) -> str | None:
    try:
        from app.agents.adzump._shared import upload_and_analyze

        result = await upload_and_analyze(
            image_bytes,
            mime_type,
            source_name,
            kind,
            context,
        )
        return (result or {}).get("url")
    except Exception as e:
        logger.warning("Upload failed for %s: %s", source_name, e)
        return None


async def _download_image(url: str, as_png: bool = False) -> tuple[bytes, str]:
    import httpx
    from PIL import Image
    import io
    from app.config import settings

    if url.startswith("/"):
        url = f"{settings.GATEWAY_URL.rstrip('/')}{url}"

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "image/jpeg")
        data = resp.content

        if as_png:
            try:
                img = Image.open(io.BytesIO(data))
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                return buf.getvalue(), "image/png"
            except Exception as e:
                logger.warning(f"Failed to convert image to PNG: {e}")

        return data, ctype
