from __future__ import annotations

import logging
import os
from pathlib import Path
from datetime import datetime, timezone

from app.core.tools.base import ToolResult
from app.agents.adzump._shared import emit_progress, extract_json
from app.agents.adzump.agents.creative_generator.image_utils import (
    download_and_normalize_logo,
    get_base_image_b64,
)
from app.agents.adzump.agents.creative_generator.models import AdCopyOutput
from app.agents.adzump.agents.creative_generator.imagen_api import call_gemini_imagen
from app.agents.adzump.services.business_storage import save_campaign
from app.config import settings
from app.services.llm_provider import get_llm_provider

logger = logging.getLogger(__name__)


async def modify_existing_creative_workflow(service, params: dict) -> ToolResult:
    """Modify, update, or regenerate formats for a specific existing creative."""
    if service.auth is None:
        return ToolResult(success=False, error="Authentication required.")

    api_key = os.environ.get("GEMINI_API_KEY") or settings.GOOGLE_API_KEY
    if not api_key:
        return ToolResult(
            success=False,
            error="GEMINI_API_KEY is not configured. Please add it to variables.sh.",
        )

    provider_name = (
        service.context.get("provider")
        or getattr(settings, "ADZUMP_PROVIDER", settings.LLM_PROVIDER)
        or "openai"
    )

    ad_copy_list = service.spec.get("ad_copy") or []
    if not isinstance(ad_copy_list, list):
        ad_copy_list = [ad_copy_list] if ad_copy_list else []

    target_creative_index = params.get("target_creative_index")
    if target_creative_index is None or target_creative_index <= 0:
        return ToolResult(
            success=False,
            error="Parameter target_creative_index is required for modifications and must be greater than 0.",
        )

    if target_creative_index > len(ad_copy_list):
        return ToolResult(
            success=False,
            error=f"Invalid target_creative_index: {target_creative_index}. There are only {len(ad_copy_list)} creatives.",
        )

    await emit_progress(
        service.context,
        f"Regenerating targeted creative {target_creative_index}...",
    )

    existing_creative = ad_copy_list[target_creative_index - 1]
    copy_dict = dict(existing_creative)

    target_formats_str = params.get("target_formats")
    target_formats = None
    if target_formats_str:
        target_formats = [
            f.strip().lower() for f in target_formats_str.split(",") if f.strip()
        ]
    else:
        # Default to formats currently present
        target_formats = list(existing_creative.get("creative_urls", {}).keys())

    # Resolve base background image
    base_img_path = params.get("custom_background_image") or existing_creative.get(
        "base_image_url"
    )
    base_b64, base_mime = None, None
    if base_img_path:
        logger.info("Downloading base background image: %s", base_img_path)
        base_b64_res = await get_base_image_b64(
            base_img_path, service.client, service.headers
        )
        if base_b64_res:
            base_b64, base_mime = base_b64_res

    # If any text/theme changes are requested, re-run copywriting to synthesize updated art direction
    if any(
        params.get(k)
        for k in (
            "custom_headline",
            "custom_description",
            "custom_cta",
            "custom_theme",
        )
    ):
        await emit_progress(
            service.context,
            "Re-evaluating creative copywriting and art direction layout...",
        )
        try:
            prev_url = params.get("edited_creative_url") or existing_creative.get(
                "creative_urls", {}
            ).get("square")
            prev_b64, prev_mime = None, None
            if prev_url:
                prev_res = await get_base_image_b64(
                    prev_url, service.client, service.headers
                )
                if prev_res:
                    prev_b64, prev_mime = prev_res

            is_real_estate = False
            if service.session:
                from app.agents.adzump.agent import CampaignContext

                cctx = CampaignContext.from_session(service.session)
                is_real_estate = cctx.is_real_estate

            business_type = service.product_data.get("business_type") or "business"
            product_name = (
                service.product_data.get("product_name")
                or service.spec.get("product_name")
                or "our product"
            )

            prompts_dir = Path(__file__).resolve().parent / "prompts"
            system_prompt = (prompts_dir / "creative_copy.txt").read_text(
                encoding="utf-8"
            )

            edit_instructions = "The user is editing an existing creative. Adjust copy and layout composition accordingly.\n"
            if params.get("custom_headline"):
                edit_instructions += (
                    f"- Change Headline to: {params['custom_headline']}\n"
                )
            if params.get("custom_description"):
                edit_instructions += (
                    f"- Change Description to: {params['custom_description']}\n"
                )
            if params.get("custom_cta"):
                edit_instructions += f"- Change CTA to: {params['custom_cta']}\n"
            if params.get("custom_theme"):
                edit_instructions += f"- Apply Visual Theme: {params['custom_theme']}\n"
            if not prev_b64 and not base_b64:
                if is_real_estate:
                    fallback_desc = "a high-end luxury modern interior or exterior property background photo with warm natural lighting"
                else:
                    fallback_desc = f"a premium, professional studio background scene showcasing {product_name or 'the product'} suited for a {business_type or 'high-end'} brand"
                edit_instructions += (
                    f"\nNOTE: No base background image is attached because the original assets were not suitable. "
                    f"Assume a scene of {fallback_desc} and write the copy/layout details for that scene."
                )
            provider = get_llm_provider(provider_name)
            user_content = []
            if prev_b64 and prev_mime:
                user_content.append(
                    {
                        "type": "text",
                        "text": "Image A (Previously Generated Creative Layout Reference):\n",
                    }
                )
                user_content.append(provider.format_image_content(prev_b64, prev_mime))
            if base_b64 and base_mime:
                user_content.append(
                    {
                        "type": "text",
                        "text": "\nImage B (Original Clean Background Photo):\n",
                    }
                )
                user_content.append(provider.format_image_content(base_b64, base_mime))

            user_content.append(
                {
                    "type": "text",
                    "text": (
                        f"{edit_instructions}\n"
                        f"Original context info:\n"
                        f"Product Name: {service.product_data.get('product_name', 'our product')}\n"
                        f"Summary: {service.product_data.get('summary', '')}\n"
                        f"Return the updated creative JSON with keys: "
                        f"'headline', 'description', 'cta', 'design_composition', 'color_palette_and_theme', 'image_prompt', 'rera_no', 'price', 'location'."
                    ),
                }
            )

            response = await provider.create_completion(
                system_prompt=system_prompt,
                messages=[{"role": "user", "content": user_content}],
                model_tier="balanced",
                max_tokens=1500,
            )
            content = response.get("content", "")
            data = extract_json(content)
            if data:
                validated_copy = AdCopyOutput(**data)
                copy_dict.update(
                    validated_copy.model_dump()
                    if hasattr(validated_copy, "model_dump")
                    else validated_copy.dict()
                )
        except Exception as e:
            logger.warning("Failed to run edit copywriting re-evaluation: %s", e)
            if params.get("custom_headline"):
                copy_dict["headline"] = params["custom_headline"]
            if params.get("custom_description"):
                copy_dict["description"] = params["custom_description"]
            if params.get("custom_cta"):
                copy_dict["cta"] = params["custom_cta"]
            if params.get("custom_theme"):
                copy_dict["image_prompt"] += (
                    f" Use a {params['custom_theme']} visual style."
                )
    else:
        if params.get("custom_headline"):
            copy_dict["headline"] = params["custom_headline"]
        if params.get("custom_description"):
            copy_dict["description"] = params["custom_description"]
        if params.get("custom_cta"):
            copy_dict["cta"] = params["custom_cta"]
        if params.get("custom_theme"):
            copy_dict["image_prompt"] += (
                f" Use a {params['custom_theme']} visual style."
            )

    current_rera = copy_dict.get("rera_no")
    if current_rera and current_rera.lower().strip() in (
        "not found",
        "not available",
        "none",
        "null",
    ):
        copy_dict["rera_no"] = ""
    logo_url = service.product_data.get("logo_url")
    logger.info("Downloading logo from URL: %s", logo_url)
    logo_b64, logo_mime = await download_and_normalize_logo(
        logo_url,
        service.client,
        service.headers,
        service.context,
    )

    history_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "headline": existing_creative.get("headline", ""),
        "description": existing_creative.get("description", ""),
        "cta": existing_creative.get("cta", ""),
        "creative_urls": dict(existing_creative.get("creative_urls", {})),
        "design_composition": existing_creative.get("design_composition", ""),
        "color_palette_and_theme": existing_creative.get("color_palette_and_theme", ""),
    }
    copy_dict.setdefault("history", []).append(history_entry)

    new_creative_res = await call_gemini_imagen(
        base_b64,
        base_mime,
        base_img_path,
        existing_creative.get("creative_type", "own"),
        logo_b64,
        logo_mime,
        copy_dict,
        api_key,
        service.context,
        target_formats=target_formats,
    )

    existing_urls = existing_creative.get("creative_urls") or {}
    new_urls = new_creative_res.get("creative_urls") or {}
    merged_urls = {**existing_urls, **new_urls}
    copy_dict["creative_urls"] = merged_urls
    copy_dict["base_image_url"] = new_creative_res.get("base_image_url") or ""

    ad_copy_list[target_creative_index - 1] = copy_dict
    service.spec["ad_copy"] = ad_copy_list
    service.spec["creative_approved"] = None
    service.sctx["campaign_spec"] = service.spec

    logger.info("Auto-saving creative modifications to storage...")
    await save_campaign(service.sctx, service.context)



    await emit_progress(
        service.context,
        f"Ad creative {target_creative_index} updated and saved successfully.",
    )
    return ToolResult(
        success=True,
        data={"creatives": ad_copy_list},
        summary=f"Successfully updated Creative {target_creative_index}",
    )
