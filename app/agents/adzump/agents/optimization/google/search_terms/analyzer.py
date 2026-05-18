from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.services.llm_provider import get_llm_provider
from app.config import settings

logger = logging.getLogger(__name__)

class SearchTermAnalyzer:
    def __init__(self, provider_name: str | None = None):
        self.provider = get_llm_provider(provider_name or getattr(settings, "ADZUMP_PROVIDER", settings.LLM_PROVIDER))
        self.prompts_dir = Path(__file__).parent / "prompts"

    def _load_prompt_part(self, filename: str) -> str:
        """Load a prompt file and extract the rule section."""
        path = self.prompts_dir / filename
        if not path.exists():
            return ""
        
        content = path.read_text(encoding="utf-8")
        
        # Extract the section between SYSTEM_PROMPT: and USER_PROMPT: if they exist
        if "SYSTEM_PROMPT:" in content:
            parts = content.split("SYSTEM_PROMPT:")[1]
            if "USER_PROMPT:" in parts:
                return parts.split("USER_PROMPT:")[0].strip()
            return parts.strip()
        
        return content.strip()

    def _build_master_prompt(self, campaign_name: str, business_summary: str, search_terms_json: str) -> str:
        # Load individual rules from your .txt files
        brand_rules = self._load_prompt_part("brand_relevancy_prompt.txt")
        location_rules = self._load_prompt_part("location_relevancy_prompt.txt")
        config_rules = self._load_prompt_part("configuration_relevancy_prompt.txt")
        
        return f"""
You are an expert Google Ads Specialist. Your task is to analyze a list of search terms against a business summary and determine if each term should be added as a keyword (positive), excluded as a negative keyword (negative), or ignored (neutral).

CAMPAIGN NAME:
{campaign_name}

BUSINESS SUMMARY:
{business_summary}

SEARCH TERMS TO ANALYZE:
{search_terms_json}

---
CORE EVALUATION RULES (Loaded from System Config):

### BRAND RULES:
{brand_rules}

### LOCATION RULES:
{location_rules}

### CONFIGURATION RULES:
{config_rules}

---
ADDITIONAL OVERRIDE RULES:
- BRAND PROTECTION: If the search term contains words from the CAMPAIGN NAME (e.g. "Keya" for "Keya Brand Campaign"), it is likely a brand keyword. Mark it as "positive" even if the business summary match is low.
- If it's highly relevant with purchase intent → positive.
- If it's too broad, irrelevant, or a competitor brand (and NOT your own brand) -> negative.

OUTPUT FORMAT:
Return a JSON object with a "results" key containing a list of evaluations.
Each evaluation must have:
- text: The original search term.
- recommendation_type: "positive" or "negative".
- reason: A high-level summary of why this recommendation was made.
- analysis: A nested object with:
    - brand: {{ "match": bool, "type": "own_brand"|"competitor"|"generic", "competitor_detected": bool, "match_level": "Strong Match"|..., "reason": "str" }}
    - configuration: {{ "match": bool, "score": float, "match_level": "...", "reason": "str" }}
    - location: {{ "match": bool, "match_level": "...", "reason": "str" }}
    - strength: "HIGH" | "MEDIUM" | "LOW"
"""

    async def analyze(self, business_summary: str, search_terms: list[dict], campaign_name: str = "Unknown") -> list[dict]:
        if not search_terms:
            return []

        # Prepare a simplified list of terms for the LLM to save tokens
        terms_to_analyze = []
        for term in search_terms:
            terms_to_analyze.append({
                "text": term.get("search_term"),
                "metrics": term.get("metrics", {})
            })

        prompt = self._build_master_prompt(
            campaign_name=campaign_name,
            business_summary=business_summary,
            search_terms_json=json.dumps(terms_to_analyze, indent=2)
        )

        try:
            response = await self.provider.create_completion(
                system_prompt="You are a Google Ads optimization expert. Return only valid JSON.",
                messages=[{"role": "user", "content": prompt}],
                model_tier="balanced",
                max_tokens=4000
            )

            content = response["content"].strip()
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            
            data = json.loads(content)
            return data.get("results", [])

        except Exception as e:
            logger.exception("Failed to analyze search terms with LLM")
            return []

async def get_search_term_analyzer() -> SearchTermAnalyzer:
    return SearchTermAnalyzer()
