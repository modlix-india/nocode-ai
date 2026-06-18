# app/agents/adzump/agents/campaign/meta/audience_planner_agent.py
"""Meta Campaign Audience Planning agent."""

import logging
from app.core.agent import BaseAgent
from app.agents.adzump.agents.campaign.meta.models import (
    BusinessProfileInput,
    MetaAudiencePlan,
    RecommendationDetail,
)
from app.agents.adzump.agents.campaign.meta.prompts.audience_prompt import (
    META_AUDIENCE_PLAN_SYSTEM_PROMPT,
)
from app.agents.adzump.tools._shared import extract_json
from app.services.llm_provider import get_llm_provider

logger = logging.getLogger(__name__)

# Absolute Meta Ads targeting restrictions
META_MIN_AGE = 13
META_MAX_AGE = 65

# Pre-launch default campaign targeting age boundaries
DEFAULT_MIN_AGE = 18
DEFAULT_MAX_AGE = 65

# Gender normalization dictionary mapping
GENDER_NORMALIZATION_MAP = {
    "male": "male",
    "men": "male",
    "female": "female",
    "women": "female",
}



class MetaAudiencePlannerAgent(BaseAgent):
    """Planner agent that recommends demographic targeting for Meta Ads campaign creation."""

    display_name = "Meta Audience Planner"

    def __init__(self, provider: str | None = None) -> None:
        # Recommender is stateless and has no tool dependencies.
        from app.core.context import BaseContext
        from app.config import settings

        provider_name = provider or getattr(
            settings, "ADZUMP_PROVIDER", settings.LLM_PROVIDER
        )
        context_builder = BaseContext(doc_paths=[], static_prefix="")
        super().__init__(
            name="meta_audience_planner",
            tools=[],
            context_builder=context_builder,
            provider=provider_name,
        )

    async def plan_audience(self, profile: BusinessProfileInput) -> MetaAudiencePlan:
        """Call the LLM to generate demographic recommendations based on the business profile.

        Args:
            profile: Input parameters describing the business profile and campaign objective.

        Returns:
            MetaAudiencePlan containing recommended demographics, reasoning, and assumptions.
        """
        # Format the user inputs cleanly as instructed by the planning guidelines.
        prices_str = ", ".join(profile.prices) if profile.prices else "Not provided"
        locations_str = (
            ", ".join(profile.locations) if profile.locations else "Not provided"
        )

        user_content = f"""### INPUT DATA
- Business Type: {profile.businessType}
- Business Summary: {profile.businessSummary}
- Locations: {locations_str}
- Prices: {prices_str}
- Campaign Objective: {profile.campaignObjective or "Not provided"}"""

        logger.info(
            "Calling MetaAudiencePlannerAgent with businessType=%s",
            profile.businessType,
        )

        provider = get_llm_provider(self._provider_name)
        completion = await provider.create_completion(
            system_prompt=META_AUDIENCE_PLAN_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
            model_tier="balanced",
        )

        raw_text = completion.get("content", "")
        payload = extract_json(raw_text)

        if not payload:
            logger.error(
                "Failed to extract JSON from audience planner agent completion: %s",
                raw_text,
            )
            raise ValueError("LLM response did not contain a valid JSON object.")

        # Extract values from the JSON output payload
        recommendation_data = payload.get("recommendation") or {}
        confidence = payload.get("confidence", 0.5)
        reasoning = payload.get("reasoning") or []

        # Enforce validation and clamping constraints on age targets
        age_min = recommendation_data.get("ageMin")
        age_max = recommendation_data.get("ageMax")

        age_min = (
            max(META_MIN_AGE, min(META_MAX_AGE, int(age_min)))
            if age_min is not None
            else DEFAULT_MIN_AGE
        )
        age_max = (
            max(META_MIN_AGE, min(META_MAX_AGE, int(age_max)))
            if age_max is not None
            else DEFAULT_MAX_AGE
        )

        # If inverted, swap parameters
        if age_min > age_max:
            logger.warning(
                "Swapping inverted age bounds: ageMin=%d, ageMax=%d", age_min, age_max
            )
            age_min, age_max = age_max, age_min

        # Enforce normalization constraints on gender target
        raw_gender = str(recommendation_data.get("gender") or "all").strip().lower()
        gender = GENDER_NORMALIZATION_MAP.get(raw_gender, "all")

        # Assemble normalized data structures
        recommendation = RecommendationDetail(
            ageMin=age_min,
            ageMax=age_max,
            gender=gender,
        )

        return MetaAudiencePlan(
            recommendation=recommendation,
            confidence=confidence,
            reasoning=reasoning,
        )
