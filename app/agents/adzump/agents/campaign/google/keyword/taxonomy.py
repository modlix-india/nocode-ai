"""Offering-taxonomy derivation for keyword research.

product_data has no persisted category/sibling taxonomy, so we derive one ONCE per
run from the user-confirmed fields (business_type, products/services, USPs, summary).
It gives the agent its offering boundary: what the business sells (core) vs adjacent
same-industry categories it does NOT sell (siblings).

Business-agnostic (derived per business, never hardcoded). One balanced-tier LLM call
(AsyncOpenAI one-shot, JSON mode), no web search. Fail-soft: any failure returns a
minimal taxonomy so research never blocks.
"""

from __future__ import annotations

import logging

from app.agents.adzump._shared import extract_json
from app.agents.adzump.agents.campaign.google.keyword.models import OfferingTaxonomy
from app.config import settings
from app.core.session import record_oneshot_usage

logger = logging.getLogger(__name__)

# A reasoning model spends output tokens deliberating before it emits the JSON, so the budget
# covers both — too small and the object is truncated into the minimal fallback.
_MAX_TOKENS = settings.AGENT_MAX_TOKENS

_SYSTEM = "You are a paid-search strategist. Output strict JSON only — no markdown, no commentary."

_PROMPT = """\
A business is running a Google Search ad campaign. From its CONFIRMED details below,
define the offering boundary that keyword targeting must respect.

BUSINESS:
{brief}

Return JSON exactly in this shape:
{{
  "primary_offering": "the crisp category a buyer shops for (buyer's words, not the seller's marketing label)",
  "brand_terms": ["the word(s) a buyer types to mean THIS company specifically"],
  "core_terms": ["the actual products/services they sell — terms a buyer would type"],
  "sibling_categories": ["adjacent categories in the SAME industry they do NOT sell"],
  "is_location_specific": true,
  "includes_informational_funnel": false
}}

Rules:
- brand_terms name the COMPANY, never its industry — drop any generic word that happens to sit
  in the company name: "Kajaria Ceramics" -> ["kajaria"] (ceramics is the industry, and a buyer
  typing it does not mean this company); "Sumadhura Group" -> ["sumadhura"]. Empty if the name
  is entirely generic.
- core_terms MUST come from the business details — never invent an offering they don't have.
- sibling_categories are same-industry NEIGHBOURS the business does NOT offer (what buyers confuse it with),
  so they can be kept out of positives and used as negatives.
- is_location_specific: true if the business serves specific geographic areas (local/regional — a shop,
  clinic, real-estate project, city service); false if it sells nationally or online with no served area.
- includes_informational_funnel: true ONLY if buyers commonly research this offering through how-to /
  tutorial / comparison / educational content BEFORE buying (considered or learning-curve purchases).
  false for plain transactional or local intent with no research phase (a standing desk or a no-code
  crm -> true; a taxi ride or a pizza place -> false).
- Works for ANY business. The core-vs-sibling distinction, by example:
  - duplex villaments -> core: villament, duplex villa, 3 bhk villa | siblings: apartment, flat, plot, independent house
  - no-code CRM -> core: no-code crm, sales pipeline software | siblings: erp, helpdesk, marketing automation
  - italian restaurant -> core: italian restaurant, wood-fired pizza | siblings: cafe, bar, fast food
- All lowercase. 4-10 entries each. No duplicates.
"""


def _brief(product: dict) -> str:
    """Compact business brief from the confirmed product_data fields."""
    parts: list[str] = []
    if v := product.get("business_type"):
        parts.append(f"Type: {v}")
    if v := product.get("products_services"):
        parts.append("Products / services: " + "; ".join(str(s) for s in v))
    if v := product.get("unique_features"):
        parts.append("USPs: " + "; ".join(str(s) for s in v))
    if v := product.get("summary"):
        parts.append("Summary: " + str(v))
    return "\n".join(parts)


async def derive_offering_taxonomy(product: dict) -> OfferingTaxonomy:
    """Derive the offering taxonomy from confirmed product_data. Fail-soft: on empty
    input or any error, returns a minimal taxonomy (primary_offering from business_type).
    """
    # core_terms must be non-empty so a failed derivation still anchors the agent.
    primary = (
        product.get("business_type") or product.get("product_name") or ""
    ).strip()
    fallback = OfferingTaxonomy(
        primary_offering=primary,
        core_terms=[primary] if primary else [],
    )
    brief = _brief(product)
    if not brief.strip():
        return fallback

    try:
        from openai import AsyncOpenAI

        # DeepSeek via the OpenAI-compatible client (same API; different base_url/key/model).
        client = AsyncOpenAI(
            api_key=settings.DEEPSEEK_API_KEY, base_url=settings.DEEPSEEK_BASE_URL
        )
        model = settings.DEEPSEEK_MODEL_BALANCED
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _PROMPT.format(brief=brief)},
            ],
            temperature=0,
            max_tokens=_MAX_TOKENS,
            response_format={"type": "json_object"},
        )
        # Bill this one-shot to the active agent's session (the loop can't see it);
        # `step` gives it its own line in per-agent cost breakdowns.
        if resp.usage is not None:
            await record_oneshot_usage(
                {
                    "input_tokens": resp.usage.prompt_tokens,
                    "output_tokens": resp.usage.completion_tokens,
                },
                model,  # bill the model actually used
                step="offering_taxonomy",
            )
        # extract_json tolerates a ```json fence and returns None (never raises) on
        # unparseable output; return the fallback so core_terms stays non-empty.
        data = extract_json(resp.choices[0].message.content or "")
        if not data:
            logger.warning("offering_taxonomy: no parseable JSON (fail-soft)")
            return fallback.model_copy(update={"complete": False})
        taxonomy = OfferingTaxonomy(
            primary_offering=str(
                data.get("primary_offering") or fallback.primary_offering
            ).strip(),
            brand_terms=list(data.get("brand_terms") or []),
            core_terms=list(data.get("core_terms") or []),
            sibling_categories=list(data.get("sibling_categories") or []),
            is_location_specific=bool(data.get("is_location_specific", True)),
            includes_informational_funnel=bool(
                data.get("includes_informational_funnel", False)
            ),
        )
        logger.info(
            "offering_taxonomy: primary=%r brand=%s core=%d siblings=%d local=%s informational=%s",
            taxonomy.primary_offering,
            taxonomy.brand_terms,
            len(taxonomy.core_terms),
            len(taxonomy.sibling_categories),
            taxonomy.is_location_specific,
            taxonomy.includes_informational_funnel,
        )
        return taxonomy
    except Exception as exc:
        logger.warning(
            "offering_taxonomy derivation failed (fail-soft): %s", str(exc)[:200]
        )
        return fallback.model_copy(update={"complete": False})
