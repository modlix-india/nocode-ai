from __future__ import annotations

import logging
from app.agents.adzump._shared import extract_json
from app.agents.adzump.agents.creative_generator.models import AdCopyOutput
from app.services.llm_provider import get_llm_provider

logger = logging.getLogger(__name__)


async def generate_one_ad_copy(
    base_b64: str | None,
    base_mime: str | None,
    base_user_msg: str,
    system_prompt: str,
    persona: str,
    category: str,
    comp_recipe: str = "",
    is_real_estate: bool = False,
    price: str = "",
    location: str = "",
    rera_info: str = "",
    business_type: str = "",
    product_name: str = "",
    params: dict = None,
    provider_name: str = "openai",
) -> dict:
    """Call LLM directly to generate ad copy for a specific persona and category."""
    provider = get_llm_provider(provider_name)

    user_msg = base_user_msg
    user_msg += f"\nTARGET AUDIENCE PERSONA: {persona.upper()}\n"
    user_msg += f"Write ad copy hooks and layout arrangements tailored specifically for a {persona} target audience.\n"

    if category == "competitor" and comp_recipe:
        user_msg += (
            f"\nCOMPETITOR STYLE REFERENCE STYLE RECIPE:\n"
            f"Extract styling structural framing and messaging angles inspired by this recipe, but write unique copy for the target persona:\n"
            f"{comp_recipe}\n"
        )

    if not base_b64:
        if is_real_estate:
            fallback_desc = "a high-end luxury modern interior or exterior property background photo with warm natural lighting"
        else:
            fallback_desc = f"a premium, professional studio background scene showcasing {product_name or 'the product'} suited for a {business_type or 'high-end'} brand"
        user_msg += (
            f"\nNOTE: No base background image is attached because the original assets were not suitable. "
            f"Assume a scene of {fallback_desc} and write the copy/layout details for that scene."
        )

    user_content = []
    if base_b64 and base_mime:
        user_content.append(provider.format_image_content(base_b64, base_mime))
    user_content.append({"type": "text", "text": user_msg})

    response = await provider.create_completion(
        system_prompt=system_prompt,
        messages=[{"role": "user", "content": user_content}],
        model_tier="balanced",
        max_tokens=1500,
    )
    content = response.get("content", "")
    data = extract_json(content)
    if not data:
        logger.error("Failed to parse JSON. Raw content: %s", content)
        raise ValueError(f"Failed to parse ad copy JSON for persona: {persona}")

    validated_copy = AdCopyOutput(**data)
    copy_dict = (
        validated_copy.model_dump()
        if hasattr(validated_copy, "model_dump")
        else validated_copy.dict()
    )

    if params:
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

    # Inject real estate fallback values
    if is_real_estate:
        if not copy_dict.get("price"):
            copy_dict["price"] = price
        if not copy_dict.get("location"):
            copy_dict["location"] = location

        current_rera = copy_dict.get("rera_no")
        if not current_rera:
            current_rera = rera_info

        # Sanitize RERA: empty string if missing, "not found", or "not available"
        if not current_rera or current_rera.lower().strip() in (
            "not found",
            "not available",
            "none",
            "null",
        ):
            copy_dict["rera_no"] = ""
        else:
            copy_dict["rera_no"] = current_rera

    copy_dict["creative_type"] = category
    copy_dict["target_persona"] = persona
    return copy_dict
