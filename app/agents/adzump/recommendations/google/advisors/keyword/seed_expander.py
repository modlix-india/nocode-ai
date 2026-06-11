"""Keyword Seed Expander.

Uses LLM to expand a small list of high-performing keywords into a broader
list of seed keywords, further expanded using Google Ads autocomplete.
"""

import asyncio
import logging

from app.core.session import BaseSession
from app.services.llm_provider import get_llm_provider
from app.agents.adzump.adapters.google.autocomplete import (
    batch_fetch_autocomplete_suggestions,
)
from app.agents.adzump.recommendations.google.advisors._utils import clean_and_load_json
from app.agents.adzump.utils.prompt_loader import load_prompt

logger = logging.getLogger(__name__)

LLM_SEED_COUNT = 10
AUTOCOMPLETE_MAX_SUGGESTIONS = 5


class KeywordSeedExpander:
    async def expand_seeds(
        self,
        good_keywords: list[str],
        business_type: str,
        primary_location: str,
        features_context: str,
        brand_name: str = "",
        session: BaseSession | None = None,
    ) -> list[str]:
        original_seeds = self._deduplicate(good_keywords)

        llm_seeds, autocomplete_from_originals = await asyncio.gather(
            self._generate_llm_seeds(
                original_seeds,
                business_type,
                primary_location,
                features_context,
                brand_name,
                session=session,
            ),
            self._expand_with_autocomplete(original_seeds),
        )

        seen = {s.lower() for s in original_seeds}
        new_llm_seeds = self._deduplicate(llm_seeds, seen)

        autocomplete_from_llm = await self._expand_with_autocomplete(new_llm_seeds)

        all_seeds = (
            original_seeds
            + new_llm_seeds
            + autocomplete_from_originals
            + autocomplete_from_llm
        )
        return self._deduplicate(all_seeds)

    async def _generate_llm_seeds(
        self,
        performing_keywords: list[str],
        business_type: str,
        primary_location: str,
        features_context: str,
        brand_name: str = "",
        session: BaseSession | None = None,
    ) -> list[str]:
        try:
            template = load_prompt("recommendations/seed_expansion_prompt.txt")
            keywords_text = ", ".join(performing_keywords[:15])
            fc = f"- Unique Features: {features_context}" if features_context else ""
            prompt = template.format(
                performing_keywords=keywords_text,
                brand_name=brand_name or "Unknown",
                business_type=business_type or "Unknown",
                primary_location=primary_location or "Unknown",
                features_context=fc,
                count=LLM_SEED_COUNT,
            )

            provider = get_llm_provider("openai")
            response = await provider.create_completion(
                system_prompt="You are a Google Ads expert. Return JSON only.",
                messages=[{"role": "user", "content": prompt}],
                model_tier="fast",
                max_tokens=500,
            )

            from app.agents.adzump.recommendations.google.advisors._utils import (
                track_advisor_llm_call,
            )

            await track_advisor_llm_call(
                session=session,
                response=response,
                prefix="kw_seed",
                provider_name=provider.name.lower()
                if hasattr(provider, "name")
                else "openai",
            )

            data = clean_and_load_json(response.get("content", ""))
            seeds = data.get("keywords", [])
            logger.info("LLM seed generation complete", count=len(seeds))
            return seeds
        except Exception as e:
            logger.error("LLM seed generation failed", error=str(e))
            return []

    async def _expand_with_autocomplete(self, seeds: list[str]) -> list[str]:
        if not seeds:
            return []
        try:
            return await batch_fetch_autocomplete_suggestions(
                seeds, max_results_per_seed=AUTOCOMPLETE_MAX_SUGGESTIONS
            )
        except Exception as e:
            logger.warning("autocomplete_expansion_failed seeds=%d error=%s", len(seeds), e)
            return []

    def _deduplicate(self, seeds: list[str], seen: set[str] | None = None) -> list[str]:
        seen = set(seen) if seen else set()
        unique = []
        for s in seeds:
            key = s.lower().strip()
            if key and key not in seen:
                seen.add(key)
                unique.append(s)
        return unique


seed_expander = KeywordSeedExpander()
