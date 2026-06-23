"""Core implementation of creative copy generation and image creation services."""

from __future__ import annotations

import logging
import os
import asyncio
from enum import Enum
from pathlib import Path

from app.core.tools.base import ToolResult
from app.agents.appbuilder.tools._shared import get_saas_client
from app.agents.adzump._shared import build_ds_headers, emit_progress, extract_json
from app.agents.adzump.agents.creative_generator.agent import (
    get_creative_selection_agent,
)
from app.agents.adzump.agents.creative_generator.models import (
    AdCopyOutput,
    ImageSelectionOutput,
)
from app.agents.adzump.agents.creative_generator.config_parser import (
    parse_creative_counts,
    filter_competitor_images,
)
from app.agents.adzump.agents.creative_generator.image_utils import (
    download_and_normalize_logo,
    get_base_image_b64,
)
from app.agents.adzump.agents.creative_generator.imagen_api import call_gemini_imagen
from app.config import settings
from app.services.llm_provider import get_llm_provider

logger = logging.getLogger(__name__)


class CreativeType(str, Enum):
    OWN = "own"
    COMPETITOR = "competitor"


class CreativeGenerationService:
    """Service to handle the orchestration of creative copywriting and image generation."""

    def __init__(self, context: dict) -> None:
        self.context = context
        self.session = context.get("_session")
        self.sctx = (
            self.session.context
            if self.session
            else (context.get("session_context") or {})
        )
        self.product_data = self.sctx.get("product_data") or {}
        self.competitor_analysis = self.sctx.get("competitor_analysis") or {}
        self.spec = self.sctx.get("campaign_spec") or {}
        self.auth = context.get("auth")
        self.stream = context.get("event_stream")
        self.tool_use_id = context.get("tool_use_id", "")
        self.client = get_saas_client()
        self.headers = build_ds_headers(context)

    async def select_best_image(
        self, pool: list[str], category: CreativeType
    ) -> str | None:
        """Use the CreativeSelectionAgent to choose the single best base image from candidate pool."""
        if not pool:
            return None
        if len(pool) == 1:
            return pool[0]

        candidates = pool[:3]
        await emit_progress(
            self.context,
            f"Selecting the best candidate from {category.value} images...",
        )

        candidate_parts = []
        downloaded_paths = []
        for path in candidates:
            res_b64 = await get_base_image_b64(path, self.client, self.headers)
            if res_b64:
                candidate_parts.append(res_b64)
                downloaded_paths.append(path)

        if not candidate_parts:
            return None

        try:
            selection_agent = get_creative_selection_agent()

            user_msg = "Here are the candidate background images to choose from:"
            user_msg += (
                f"\nReturn a JSON object containing the index (0, 1, or 2) of the best background image "
                f"suitable for a Facebook ad creative of a {self.product_data.get('business_type', 'business')} brand:\n"
                '{\n  "selected_index": <int: index of selected candidate>,\n'
                '  "reasoning": "brief reasoning"\n}'
            )

            completion = await selection_agent.select(
                user_message=user_msg,
                image_blocks=candidate_parts,
            )

            choice_data = extract_json(completion)
            if choice_data:
                validated_choice = ImageSelectionOutput(**choice_data)
                idx = validated_choice.selected_index
                if 0 <= idx < len(downloaded_paths):
                    return downloaded_paths[idx]
        except Exception as e:
            logger.warning(
                "Selection agent failed, falling back to first image. Error: %s", e
            )

        return pool[0]

    async def generate_ad_copy_and_prompt(self, params: dict) -> ToolResult:
        """Generate ad copy and images in a single call."""
        if self.auth is None:
            return ToolResult(success=False, error="Authentication required.")

        await emit_progress(
            self.context, "Analyzing campaign details and generating ad copy..."
        )

        business_type = self.product_data.get("business_type") or "business"
        product_name = (
            self.product_data.get("product_name")
            or self.spec.get("product_name")
            or "our product"
        )
        summary = self.product_data.get("summary") or ""
        location = self.spec.get("location") or self.product_data.get("location") or ""

        is_real_estate = False
        if self.session:
            from app.agents.adzump.agent import CampaignContext

            cctx = CampaignContext.from_session(self.session)
            is_real_estate = cctx.is_real_estate

        user_msg = (
            f"Business Name: {product_name}\n"
            f"Business Type: {business_type}\n"
            f"Summary: {summary}\n"
            f"Location: {location}\n"
        )

        if is_real_estate:
            price = (
                self.product_data.get("pricing")
                or self.spec.get("budget")
                or "premium pricing"
            )
            import re

            rera_match = re.search(
                r"(?:RERA|PRM/KA|P521000|UPRERA)[^\n\.,;]*", summary, re.IGNORECASE
            )
            rera_info = rera_match.group(0).strip() if rera_match else "Not found"

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

        user_msg += (
            "\nProvide the output in JSON format with keys: "
            "'headline', 'description', 'cta', 'image_prompt', 'rera_no'.\n"
            "Keep the headline under 40 characters, and description under 80 characters.\n"
            "The 'image_prompt' should be a detailed scene description for a 1:1 square ad creative. "
            "It must instruct the model to natively render the headline text and CTA button text inside the image. "
            "Do not include any other text output outside the JSON."
        )

        try:
            # Step 1: Call LLM directly for ad copy (no sub-agent, no streaming to UI)
            prompts_dir = Path(__file__).resolve().parent / "prompts"
            system_prompt = (prompts_dir / "creative_copy.txt").read_text(
                encoding="utf-8"
            )

            provider = get_llm_provider("openai")
            response = await provider.create_completion(
                system_prompt=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
                model_tier="balanced",
                max_tokens=1500,
            )
            content = response.get("content", "")

            data = extract_json(content)
            if not data:
                raise ValueError("Failed to parse ad copy JSON response from LLM")

            validated_copy = AdCopyOutput(**data)
            copy_dict = validated_copy.dict()

            if params.get("custom_headline"):
                copy_dict["headline"] = params["custom_headline"]
            if params.get("custom_description"):
                copy_dict["description"] = params["custom_description"]
            if params.get("custom_cta"):
                copy_dict["cta"] = params["custom_cta"]
            if params.get("custom_theme"):
                copy_dict["image_prompt"] += (
                    f" Use a {params['custom_theme']} visual style and theme."
                )

            # Inject real estate fallback values to guarantee presence of RERA & PPP details
            if is_real_estate:
                if not copy_dict.get("price"):
                    copy_dict["price"] = price
                if not copy_dict.get("location"):
                    copy_dict["location"] = location
                if (
                    not copy_dict.get("rera_no")
                    or copy_dict.get("rera_no") == "Not found"
                ):
                    copy_dict["rera_no"] = rera_info

            self.spec["ad_copy"] = copy_dict
            self.sctx["campaign_spec"] = self.spec

            logger.info("Ad copy generation step complete. Output keys: %s", list(copy_dict.keys()))

            # Step 2: Generate images immediately
            api_key = os.environ.get("GEMINI_API_KEY") or settings.GOOGLE_API_KEY
            if not api_key:
                return ToolResult(
                    success=False,
                    error="GEMINI_API_KEY is not configured. Please add it to variables.sh.",
                )

            own_count, competitor_count = parse_creative_counts(
                self.spec.get("creative_config", "1")
            )
            competitor_images = filter_competitor_images(
                self.competitor_analysis.get("competitors") or []
            )
            own_images = self.product_data.get("creative_images") or []

            logger.info(
                "Image Generation starting: own_count=%d, competitor_count=%d, own_pool_size=%d, competitor_pool_size=%d",
                own_count,
                competitor_count,
                len(own_images),
                len(competitor_images),
            )

            logo_url = self.product_data.get("logo_url")
            logger.info("Downloading logo from URL: %s", logo_url)
            logo_b64, logo_mime = await download_and_normalize_logo(
                logo_url,
                self.client,
                self.headers,
                self.context,
            )
            logger.info("Logo download complete. Mime: %s, Length: %d", logo_mime, len(logo_b64) if logo_b64 else 0)

            logger.info("Selecting best OWN background image...")
            selected_own_path = await self.select_best_image(
                own_images, CreativeType.OWN
            )
            logger.info("Selected OWN image path: %s", selected_own_path)

            logger.info("Selecting best COMPETITOR background image...")
            selected_comp_path = (
                await self.select_best_image(competitor_images, CreativeType.COMPETITOR)
                if competitor_images
                else None
            )
            logger.info("Selected COMPETITOR image path: %s", selected_comp_path)

            logger.info("Downloading base64 for OWN background image: %s", selected_own_path)
            own_b64_res = (
                await get_base_image_b64(selected_own_path, self.client, self.headers)
                if selected_own_path
                else None
            )
            own_b64, own_mime = own_b64_res if own_b64_res else (None, None)
            logger.info("OWN background image base64 resolved. Mime: %s, Length: %d", own_mime, len(own_b64) if own_b64 else 0)

            comp_b64, comp_mime = None, None
            if selected_comp_path:
                logger.info("Downloading base64 for COMPETITOR background image: %s", selected_comp_path)
                comp_b64_res = (
                    await get_base_image_b64(selected_comp_path, self.client, self.headers)
                    if selected_comp_path
                    else None
                )
                comp_b64, comp_mime = comp_b64_res if comp_b64_res else (None, None)
                logger.info("COMPETITOR background image base64 resolved. Mime: %s, Length: %d", comp_mime, len(comp_b64) if comp_b64 else 0)

            tasks = []
            for _ in range(own_count):
                tasks.append(
                    call_gemini_imagen(
                        own_b64,
                        own_mime,
                        selected_own_path,
                        CreativeType.OWN.value,
                        logo_b64,
                        logo_mime,
                        copy_dict,
                        api_key,
                        self.context,
                    )
                )
            for _ in range(competitor_count):
                tasks.append(
                    call_gemini_imagen(
                        comp_b64,
                        comp_mime,
                        selected_comp_path,
                        CreativeType.COMPETITOR.value,
                        logo_b64,
                        logo_mime,
                        copy_dict,
                        api_key,
                        self.context,
                    )
                )

            await emit_progress(
                self.context, f"Generating {len(tasks)} ad creatives via Gemini..."
            )
            generated_creatives = await asyncio.gather(*tasks)

            self.spec["ad_copy"] = generated_creatives
            self.sctx["campaign_spec"] = self.spec
            self.sctx.setdefault("product_profile", {})["creative_generated"] = True

            if self.stream:
                preview_markdown = "\n\n### Generated Ad Creatives:\n"
                for i, c in enumerate(generated_creatives, 1):
                    urls = c.get("creative_urls", {})
                    ctype = c.get("creative_type", "")
                    preview_markdown += f"\n**Creative {i} ({ctype})**  "
                    for label in ("square", "portrait", "landscape"):
                        url = urls.get(label)
                        if url:
                            preview_markdown += (
                                f'![{label}]({url})'
                                f'{{style="width: 180px; object-fit: contain; border-radius: 6px;"}} '
                            )
                    preview_markdown += "\n"
                await self.stream.emit_text(preview_markdown + "\n")

            await emit_progress(
                self.context, "Ad creative generation completed successfully."
            )
            return ToolResult(
                success=True,
                data={"creatives": generated_creatives},
                summary=f"Successfully generated {len(generated_creatives)} ad creatives.",
            )
        except Exception as e:
            logger.exception("generate_ad_copy_and_prompt failed")
            return ToolResult(
                success=False, error=f"Failed to generate ad creatives: {str(e)}"
            )


async def generate_ad_copy_and_prompt_impl(params: dict, context: dict) -> ToolResult:
    """Entry point for generating ad copy and images."""
    service = CreativeGenerationService(context)
    return await service.generate_ad_copy_and_prompt(params)
