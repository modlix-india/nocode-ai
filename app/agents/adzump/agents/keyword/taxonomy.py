"""Offering-taxonomy derivation for keyword research.

product_data has no persisted category/sibling taxonomy, so we derive one ONCE per
run from the user-confirmed fields (business_type, products/services, USPs, summary).
It gives the agent its offering boundary: what the business sells (core) vs adjacent
same-industry categories it does NOT sell (siblings).

Business-agnostic (derived per business, never hardcoded). One balanced-tier LLM call,
no web search; routed through get_llm_provider() so token usage is tracked. Fail-soft:
any failure returns a minimal taxonomy so research never blocks.
"""

from __future__ import annotations

import logging

from app.services.llm_provider import get_llm_provider

from app.agents.adzump._shared import extract_json
from app.agents.adzump.agents.keyword.models import OfferingTaxonomy

logger = logging.getLogger(__name__)

_PROVIDER = "openai"  # match the keyword agent's provider (adzump runs on OpenAI)
_MODEL_TIER = "balanced"
_MAX_TOKENS = 700

_SYSTEM = "You are a paid-search strategist. Output strict JSON only — no markdown, no commentary."

_PROMPT = """\
A business is running a Google Search ad campaign. From its CONFIRMED details below,
define the offering boundary that keyword targeting must respect.

BUSINESS:
{brief}

Return JSON exactly in this shape:
{{
  "primary_offering": "the crisp category a buyer shops for (buyer's words, not the seller's marketing label)",
  "core_terms": ["the actual products/services they sell — terms a buyer would type"],
  "sibling_categories": ["adjacent categories in the SAME industry they do NOT sell"],
  "is_location_specific": true
}}

Rules:
- core_terms MUST come from the business details — never invent an offering they don't have.
- sibling_categories are same-industry NEIGHBOURS the business does NOT offer (what buyers confuse it with),
  so they can be kept out of positives and used as negatives.
- is_location_specific: true if the business serves specific geographic areas (local/regional — a shop,
  clinic, real-estate project, city service); false if it sells nationally or online with no served area.
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


async def derive_offering_taxonomy(product: dict) -> tuple[OfferingTaxonomy, dict]:
    """Derive the offering taxonomy from confirmed product_data.

    Returns ``(taxonomy, usage)``. ``usage`` is the provider's token-usage dict for
    the caller to ``session.accumulate_usage(...)``. Fail-soft: on empty input or any
    error, returns a minimal taxonomy (primary_offering from business_type) + ``{}``.
    """
    # core_terms must be non-empty so a failed derivation still anchors the agent.
    primary = (product.get("business_type") or product.get("product_name") or "").strip()
    fallback = OfferingTaxonomy(
        primary_offering=primary,
        core_terms=[primary] if primary else [],
    )
    brief = _brief(product)
    if not brief.strip():
        return fallback, {}

    try:
        provider = get_llm_provider(_PROVIDER)
        resp = await provider.create_completion(
            system_prompt=_SYSTEM,
            messages=[{"role": "user", "content": _PROMPT.format(brief=brief)}],
            model_tier=_MODEL_TIER,
            max_tokens=_MAX_TOKENS,
        )
        # extract_json tolerates a ```json fence and returns None (never raises) on
        # unparseable output; return the fallback so core_terms stays non-empty.
        data = extract_json(resp.get("content", ""))
        if not data:
            logger.warning("offering_taxonomy: no parseable JSON (fail-soft)")
            return fallback, {}
        taxonomy = OfferingTaxonomy(
            primary_offering=str(
                data.get("primary_offering") or fallback.primary_offering
            ).strip(),
            core_terms=list(data.get("core_terms") or []),
            sibling_categories=list(data.get("sibling_categories") or []),
            is_location_specific=bool(data.get("is_location_specific", True)),
        )
        logger.info(
            "offering_taxonomy: primary=%r core=%d siblings=%d local=%s",
            taxonomy.primary_offering,
            len(taxonomy.core_terms),
            len(taxonomy.sibling_categories),
            taxonomy.is_location_specific,
        )
        return taxonomy, (resp.get("usage") or {})
    except Exception as exc:
        logger.warning(
            "offering_taxonomy derivation failed (fail-soft): %s", str(exc)[:200]
        )
        return fallback, {}
