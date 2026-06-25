from __future__ import annotations

import logging
import os
import asyncio
from pathlib import Path
import re

from app.core.tools.base import ToolResult
from app.agents.adzump._shared import emit_progress
from app.agents.adzump.agents.creative_generator.config_parser import (
    parse_creative_counts,
    filter_competitor_images,
)
from app.agents.adzump.agents.creative_generator.image_utils import (
    download_and_normalize_logo,
    get_base_image_b64,
)
from app.agents.adzump.agents.creative_generator.imagen_api import call_gemini_imagen
from app.agents.adzump.services.business_storage import save_campaign
from app.config import settings
from app.services.llm_provider import get_llm_provider
from app.agents.adzump.agents.creative_generator.selector import select_best_image
from app.agents.adzump.agents.creative_generator.copywriter import generate_one_ad_copy

logger = logging.getLogger(__name__)


async def generate_fresh_creatives_workflow(service, params: dict) -> ToolResult:
    """Generate fresh ad copy and square creatives from scratch."""
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

    await emit_progress(
        service.context, "Analyzing campaign details and selecting base images..."
    )

    competitor_images = filter_competitor_images(
        service.competitor_analysis.get("competitors") or []
    )
    own_images = service.product_data.get("creative_images") or []

    selected_own_path = await select_best_image(
        own_images,
        "own",
        service.product_data.get("business_type", "business"),
        service.client,
        service.headers,
        service.context,
        provider_name=provider_name,
    )
    logger.info("Selected OWN image path: %s", selected_own_path)

    selected_comp_path = (
        await select_best_image(
            competitor_images,
            "competitor",
            service.product_data.get("business_type", "business"),
            service.client,
            service.headers,
            service.context,
            provider_name=provider_name,
        )
        if competitor_images
        else None
    )
    logger.info("Selected COMPETITOR image path: %s", selected_comp_path)

    own_b64_res = (
        await get_base_image_b64(selected_own_path, service.client, service.headers)
        if selected_own_path
        else None
    )
    own_b64, own_mime = own_b64_res if own_b64_res else (None, None)
    logger.info(
        "OWN background image base64 resolved. Mime: %s, Length: %d",
        own_mime,
        len(own_b64) if own_b64 else 0,
    )

    comp_b64, comp_mime = None, None
    if selected_comp_path:
        comp_b64_res = (
            await get_base_image_b64(
                selected_comp_path, service.client, service.headers
            )
            if selected_comp_path
            else None
        )
        comp_b64, comp_mime = comp_b64_res if comp_b64_res else (None, None)
        logger.info(
            "COMPETITOR background image base64 resolved. Mime: %s, Length: %d",
            comp_mime,
            len(comp_b64) if comp_b64 else 0,
        )

    # If competitor base image is selected, run competitor deconstruction analysis
    comp_recipe = ""
    if selected_comp_path and comp_b64:
        await emit_progress(
            service.context,
            "Deconstructing competitor creative layout and style strategy...",
        )
        try:
            prompts_dir = Path(__file__).resolve().parent / "prompts"
            comp_prompt = (prompts_dir / "competitor_analysis.txt").read_text(
                encoding="utf-8"
            )

            provider = get_llm_provider(provider_name)
            comp_content = [
                provider.format_image_content(comp_b64, comp_mime),
                {
                    "type": "text",
                    "text": "Analyze this competitor ad and extract the Styling Recipe.",
                },
            ]
            comp_resp = await provider.create_completion(
                system_prompt=comp_prompt,
                messages=[{"role": "user", "content": comp_content}],
                model_tier="balanced",
                max_tokens=600,
            )
            comp_recipe = (comp_resp.get("content") or "").strip()
            logger.info("Competitor Styling Recipe extracted:\n%s", comp_recipe)
        except Exception as e:
            logger.warning("Failed to deconstruct competitor creative: %s", e)

    business_type = service.product_data.get("business_type") or "business"
    product_name = (
        service.product_data.get("product_name")
        or service.spec.get("product_name")
        or "our product"
    )
    summary = service.product_data.get("summary") or ""
    location = (
        service.spec.get("location") or service.product_data.get("location") or ""
    )

    is_real_estate = False
    if service.session:
        from app.agents.adzump.agent import CampaignContext

        cctx = CampaignContext.from_session(service.session)
        is_real_estate = cctx.is_real_estate

    user_msg = (
        f"Business Name: {product_name}\n"
        f"Business Type: {business_type}\n"
        f"Summary: {summary}\n"
        f"Location: {location}\n"
    )
    if comp_recipe:
        user_msg += (
            f"\nCOMPETITOR STYLE REFERENCE:\n"
            f"You MUST align the generated ad layout and copywriting style with the competitor's design essence, but adapt it cleanly for our own product:\n"
            f"{comp_recipe}\n"
        )

    if is_real_estate:
        price = (
            service.product_data.get("pricing")
            or service.spec.get("pricing")
            or service.spec.get("price")
            or service.spec.get("budget")
            or "premium pricing"
        )

        # Fallback to campaign budget if price is still empty or same as budget
        if price == service.spec.get("budget") and service.product_data.get("pricing"):
            price = service.product_data.get("pricing")

        rera_info = (
            service.product_data.get("rera_no")
            or service.product_data.get("rera")
            or service.spec.get("rera_no")
            or ""
        )
        if not rera_info:
            rera_match = re.search(
                r"(?:RERA|PRM/KA|P521000|UPRERA)[^\n\.,;]*", summary, re.IGNORECASE
            )
            rera_info = rera_match.group(0).strip() if rera_match else ""

        if rera_info:
            user_msg += (
                f"\nThis is a Real Estate project. You MUST ensure the ad copy and image prompt include the PPP details:\n"
                f"- Project Name: {product_name}\n"
                f"- Price: {price}\n"
                f"- Location: {location}\n"
                f"- RERA Registration: {rera_info}\n\n"
                f"You MUST include the verbatim RERA registration details in the 'rera_no' field. "
                f"Also, explicitly specify in the 'image_prompt' that the RERA registration number MUST be rendered on the image. "
                f"Output a concise 'location' suitable for rendering on an ad image (just city/area, max 50 characters — e.g. 'Bannerghatta Rd, Bengaluru')."
            )
        else:
            user_msg += (
                f"\nThis is a Real Estate project. You MUST ensure the ad copy and image prompt include the PPP details:\n"
                f"- Project Name: {product_name}\n"
                f"- Price: {price}\n"
                f"- Location: {location}\n"
                f"- RERA Registration: Not available (do NOT render any RERA details, RERA numbers, or RERA labels on the image or in the copy)\n\n"
                f"Set 'rera_no' to an empty string in the output JSON. "
                f"Output a concise 'location' suitable for rendering on an ad image (just city/area, max 50 characters — e.g. 'Bannerghatta Rd, Bengaluru')."
            )

    user_msg += (
        "\nProvide the output in JSON format with keys: "
        "'headline', 'description', 'cta', 'design_composition', 'color_palette_and_theme', 'image_prompt', 'rera_no', 'price', 'location'.\n"
        "Keep the headline under 40 characters, and description under 80 characters.\n"
        "The 'design_composition' should detail custom layout framing, shape elements, logo placement, and typography positioning tailored to the attached base image.\n"
        "The 'color_palette_and_theme' should list recommended color harmonies matching the image.\n"
        "The 'image_prompt' should be a detailed scene description for a 1:1 square ad creative. "
        "It must instruct the model to natively render the headline text and CTA button text inside the image. "
        "Do not include any other text output outside the JSON."
    )

    try:
        # Resolve target personas
        target_personas_str = params.get("target_personas")
        personas = (
            [p.strip().lower() for p in target_personas_str.split(",") if p.strip()]
            if target_personas_str
            else []
        )

        # Step 1: Call LLM directly to generate ad copy versions in parallel
        prompts_dir = Path(__file__).resolve().parent / "prompts"
        system_prompt = (prompts_dir / "creative_copy.txt").read_text(encoding="utf-8")

        own_count, competitor_count = parse_creative_counts(
            service.spec.get("creative_config", "1")
        )
        total_creatives = own_count + competitor_count

        logo_url = service.product_data.get("logo_url")
        logger.info("Downloading logo from URL: %s", logo_url)
        logo_b64, logo_mime = await download_and_normalize_logo(
            logo_url,
            service.client,
            service.headers,
            service.context,
        )
        logger.info(
            "Logo download complete. Mime: %s, Length: %d",
            logo_mime,
            len(logo_b64) if logo_b64 else 0,
        )

        await emit_progress(
            service.context,
            f"Generating {total_creatives} persona-targeted copywriting versions...",
        )

        copy_tasks = []
        for i in range(total_creatives):
            persona = personas[i % len(personas)] if personas else "general brand"
            category = "own" if i < own_count else "competitor"
            copy_tasks.append(
                generate_one_ad_copy(
                    base_b64=own_b64,
                    base_mime=own_mime,
                    base_user_msg=user_msg,
                    system_prompt=system_prompt,
                    persona=persona,
                    category=category,
                    comp_recipe=comp_recipe,
                    is_real_estate=is_real_estate,
                    price=price if is_real_estate else "",
                    location=location if is_real_estate else "",
                    rera_info=rera_info if is_real_estate else "",
                    business_type=business_type,
                    product_name=product_name,
                    params=params,
                    provider_name=provider_name,
                )
            )
        generated_copies = await asyncio.gather(*copy_tasks)

        # Step 2: Generate images in parallel using respective copy blueprints
        await emit_progress(
            service.context,
            f"Generating {total_creatives} ad creatives via Gemini Imagen...",
        )

        # Fresh initial creatives always generate strictly square first
        target_formats = ["square"]

        tasks = []
        for i, copy_dict in enumerate(generated_copies):
            category = "own" if i < own_count else "competitor"
            img_b64 = own_b64 if category == "own" else (comp_b64 or own_b64)
            img_mime = own_mime if category == "own" else (comp_mime or own_mime)
            img_path = (
                selected_own_path
                if category == "own"
                else (selected_comp_path or selected_own_path)
            )
            tasks.append(
                call_gemini_imagen(
                    img_b64,
                    img_mime,
                    img_path,
                    category,
                    logo_b64,
                    logo_mime,
                    copy_dict,
                    api_key,
                    service.context,
                    target_formats=target_formats,
                )
            )

        generated_creatives = await asyncio.gather(*tasks)

        service.spec["ad_copy"] = generated_creatives
        service.spec["creative_approved"] = None
        service.sctx["campaign_spec"] = service.spec
        service.sctx.setdefault("product_profile", {})["creative_generated"] = True

        # Save directly to the Database
        logger.info("Auto-saving newly generated creatives to storage...")
        await save_campaign(service.sctx, service.context)

        await emit_progress(
            service.context, "Ad creative generation completed successfully."
        )
        return ToolResult(
            success=True,
            data={"creatives": generated_creatives},
            summary=f"Successfully generated {len(generated_creatives)} ad creatives.",
        )
    except Exception as e:
        logger.exception("generate_fresh_creatives failed")
        return ToolResult(
            success=False, error=f"Failed to generate ad creatives: {str(e)}"
        )
