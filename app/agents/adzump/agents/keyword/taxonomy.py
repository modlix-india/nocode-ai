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
_COMPETITOR_MAX_TOKENS = 1800  # one JSON entry per competitor, ~3-6 competitors typical

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

_COMPETITOR_PROMPT = """\
A business wants to run competitor-conquest Google Search ads — bidding on searches that
mention a competitor by name (e.g. "CompetitorX pricing", "CompetitorX reviews"). For EACH
competitor below, define ITS OWN offering boundary: the terms someone would pair with THAT
competitor's name when searching, anchored to what THAT competitor sells — not what the
advertiser running the ads sells.

COMPETITORS:
{briefs}

Return JSON exactly in this shape — one entry per competitor, keyed by the exact name given:
{{
  "<competitor name>": {{
    "primary_offering": "the crisp category a buyer shops for from THIS competitor (buyer's words)",
    "core_terms": ["THIS competitor's actual products/services — terms a buyer would type"],
    "sibling_categories": ["adjacent categories in the SAME industry THIS competitor does NOT sell"],
    "is_location_specific": true
  }}
}}

Rules:
- core_terms MUST come from the competitor's own details below — never invent an offering they
  don't have, and never borrow another competitor's terms.
- Every competitor listed must appear as a key in the output, even if evidence is thin (fall back
  to the competitor's business_type as a minimal core_term in that case).
- is_location_specific: true if the competitor serves specific geographic areas; false if it
  sells nationally or online with no served area.
- All lowercase. 3-8 entries each. No duplicates.
"""


async def _call_llm_json(prompt: str, max_tokens: int) -> tuple[dict | None, dict]:
    """Shared LLM call + JSON extraction; (None, {}) on any failure, never raises."""
    try:
        provider = get_llm_provider(_PROVIDER)
        resp = await provider.create_completion(
            system_prompt=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            model_tier=_MODEL_TIER,
            max_tokens=max_tokens,
        )
        return extract_json(resp.get("content", "")), (resp.get("usage") or {})
    except Exception as exc:
        logger.warning("taxonomy LLM call failed (fail-soft): %s", str(exc)[:200])
        return None, {}


def _taxonomy_from_dict(data: dict, fallback: OfferingTaxonomy) -> OfferingTaxonomy:
    """Build an OfferingTaxonomy from parsed LLM JSON, filling gaps from `fallback`."""
    return OfferingTaxonomy(
        primary_offering=str(data.get("primary_offering") or fallback.primary_offering).strip(),
        core_terms=list(data.get("core_terms") or fallback.core_terms),
        sibling_categories=list(data.get("sibling_categories") or []),
        is_location_specific=bool(data.get("is_location_specific", True)),
    )


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
    """Derive the advertiser's own offering taxonomy from confirmed product_data."""
    # core_terms must be non-empty so a failed derivation still anchors the agent.
    primary = (product.get("business_type") or product.get("product_name") or "").strip()
    fallback = OfferingTaxonomy(primary_offering=primary, core_terms=[primary] if primary else [])
    brief = _brief(product)
    if not brief.strip():
        return fallback, {}

    data, usage = await _call_llm_json(_PROMPT.format(brief=brief), _MAX_TOKENS)
    if not data:
        logger.warning("offering_taxonomy: no parseable JSON (fail-soft)")
        return fallback, {}
    taxonomy = _taxonomy_from_dict(data, fallback)
    logger.info(
        "offering_taxonomy: primary=%r core=%d siblings=%d local=%s",
        taxonomy.primary_offering,
        len(taxonomy.core_terms),
        len(taxonomy.sibling_categories),
        taxonomy.is_location_specific,
    )
    return taxonomy, usage


def _competitor_brief(competitor: dict) -> str:
    """Compact per-competitor brief; prefers rich_summary, falls back to short fields."""
    parts: list[str] = []
    if v := competitor.get("business_type"):
        parts.append(f"Type: {v}")
    if v := competitor.get("rich_summary"):
        parts.append("Summary: " + str(v))
    else:
        if v := competitor.get("why_competitor"):
            parts.append("Why a competitor: " + str(v))
        if v := competitor.get("key_usps"):
            parts.append("USPs: " + "; ".join(str(s) for s in v))
        if v := competitor.get("weakness"):
            parts.append("Weakness: " + str(v))
    return "\n".join(parts)


def _fallback_competitor_taxonomy(competitor: dict) -> OfferingTaxonomy:
    primary = (competitor.get("business_type") or competitor.get("name") or "").strip()
    return OfferingTaxonomy(primary_offering=primary, core_terms=[primary] if primary else [])


async def derive_competitor_taxonomies(
    competitors: list[dict],
) -> tuple[dict[str, OfferingTaxonomy], dict]:
    """Derive one offering taxonomy per competitor — anchored to each competitor's OWN
    offering, not the advertiser's — in a single batched LLM call."""
    named = [c for c in competitors if c.get("name")]
    fallback = {str(c["name"]).strip(): _fallback_competitor_taxonomy(c) for c in named}
    if not named:
        return {}, {}

    briefs = "\n\n".join(f"{c['name']}:\n{_competitor_brief(c)}" for c in named)
    data, usage = await _call_llm_json(
        _COMPETITOR_PROMPT.format(briefs=briefs), _COMPETITOR_MAX_TOKENS
    )
    if not data or not isinstance(data, dict):
        logger.warning("competitor_taxonomies: no parseable JSON (fail-soft)")
        return fallback, {}

    result: dict[str, OfferingTaxonomy] = {}
    for name, default in fallback.items():
        entry = data.get(name)
        result[name] = _taxonomy_from_dict(entry, default) if isinstance(entry, dict) else default

    logger.info("competitor_taxonomies: derived for %d/%d competitors", len(result), len(fallback))
    return result, usage
