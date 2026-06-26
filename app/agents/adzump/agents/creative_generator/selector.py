from __future__ import annotations

import logging
from app.agents.adzump._shared import emit_progress, extract_json
from app.agents.adzump.agents.creative_generator.image_utils import get_base_image_b64
from app.agents.adzump.agents.creative_generator.selection_agent import get_creative_selection_agent
from app.agents.adzump.agents.creative_generator.models import ImageSelectionOutput

logger = logging.getLogger(__name__)


async def select_best_image(
    pool: list[str],
    category_value: str,
    business_type: str,
    client,
    headers: dict,
    context: dict,
    provider_name: str = "openai",
) -> str | None:
    """Use the CreativeSelectionAgent to choose the single best base image from candidate pool."""
    if not pool:
        return None

    candidates = pool[:3]
    await emit_progress(
        context,
        f"Selecting and verifying the best candidate from {category_value} images...",
    )

    candidate_parts = []
    downloaded_paths = []
    for path in candidates:
        res_b64 = await get_base_image_b64(path, client, headers)
        if res_b64:
            candidate_parts.append(res_b64)
            downloaded_paths.append(path)

    if not candidate_parts:
        return None

    try:
        selection_agent = get_creative_selection_agent()

        user_msg = (
            f"Candidate background images for a Facebook ad creative of a {business_type} brand. "
            "Evaluate according to the system prompt criteria and return JSON."
        )

        completion = await selection_agent.select(
            user_message=user_msg,
            image_blocks=candidate_parts,
            provider_name=provider_name,
        )

        choice_data = extract_json(completion)
        if choice_data:
            validated_choice = ImageSelectionOutput(**choice_data)
            idx = validated_choice.selected_index
            if idx == -1:
                logger.info(
                    "Selection agent rejected all candidates as unsuitable (e.g. floor plans)."
                )
                return None
            if 0 <= idx < len(downloaded_paths):
                return downloaded_paths[idx]
    except Exception as e:
        logger.warning("Selection agent failed. Error: %s", e)

    return None
