"""Category taxonomy + the fail-closed relevance gate for competitor creatives.

Two-stage classification, then a gate:

  Stage A - the PRODUCT is classified ONCE (``ensure_product_classified``) from
            the Product Analyst's own text, signals in priority order:
            businessType -> productName -> summary -> siteLinks. The result is
            stored on ``product_data`` so every ad is judged against ONE
            yardstick - re-deriving per ad would drift. A manual
            ``product_category_override`` wins and skips Stage A entirely (a
            wrong product category poisons every gate decision downstream).
  Stage B - each AD is classified by the essence analyst (vision + OCR + copy +
            landing URL; the enum lists in its prompt are generated from these
            Literals so prompt and validator can't disagree).
  Stage C - ``gate_creative`` accepts a creative only when its category exactly
            matches the product's, its confidence clears ``ACCEPT_THRESHOLD``,
            and its market doesn't contradict the product's city. Fail CLOSED:
            unknown never passes, a tie never passes, a same-market competitor
            can absolutely run an ad for something else.

Leaf module: ``models.py`` imports the Literals from here, never the reverse.
"""

from __future__ import annotations

import re
from typing import Literal

# Bump when the taxonomy changes: carried-forward essences are version-checked
# (library._carry_forward_essence), so a bump re-classifies existing records on
# their next real ingest.
TAXONOMY_VERSION = "1"
# Stage C: an ad below this classification confidence is rejected.
ACCEPT_THRESHOLD = 0.75

# The controlled vocabulary. Store the enum, never a sentence: a single
# "real estate" bucket would make the gate a no-op; too many leaves and the
# classifier gets inconsistent.
Category = Literal[
    "residential_apartment",   # multi-tower / high-rise for sale
    "residential_villa",       # villa / row-house / townhouse for sale
    "residential_plot",        # plotted development / land for sale
    "residential_township",    # large gated mixed community
    "residential_farmhouse",   # farmhouse / weekend home
    "residential_rental",      # residential lease / rental
    "residential_serviced",    # serviced apartments / co-living
    "commercial_office",       # office space for sale or lease
    "commercial_retail",       # shops / showrooms / malls
    "commercial_industrial",   # warehouse / industrial / logistics
    "commercial_coworking",    # managed / co-working desks
    "hospitality",             # hotel / resort / serviced hotel
    "senior_living",           # senior / assisted living
    "land_parcel",             # bare land, undifferentiated
    "other_real_estate",       # real estate, none of the above
    "other_industry",          # NOT real estate (auto, FMCG, travel, finance...)
    "unknown",                 # cannot be determined - never accepted
]
OfferingStage = Literal[
    "pre_launch", "under_construction", "ready_to_move", "resale", "rental", "unknown",
]
AdvertiserRole = Literal["developer", "broker", "aggregator", "unknown"]
# Which signal decided an ad's category (auditability).
CategoryMethod = Literal["ocr", "copy", "vision", "landing", "combined", ""]

# Gate rejection reasons - written verbatim into Competitor.dropped[].
CATEGORY_MISMATCH = "category_mismatch"
MARKET_MISMATCH = "market_mismatch"
LOW_CONFIDENCE = "low_confidence"
UNKNOWN_CATEGORY = "unknown_category"
NON_REAL_ESTATE = "non_real_estate"


# ── Stage A: classify the product once ────────────────────────────────────

# Ordered keyword rules, first hit wins - order encodes specificity ("2 & 3 BHK
# villas" must land on villa, not apartment). The intelligence already ran:
# the Product Analyst's LLM wrote businessType/summary; this only normalizes
# that text into the enum, so Stage A needs no model call of its own.
_KEYWORD_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("residential_township", ("township", "integrated community")),
    ("residential_villa", ("villa", "row house", "rowhouse", "row-house",
                           "townhouse", "town house")),
    ("residential_plot", ("plotted development", "plotted", "plot", "jda",
                          "sites for sale", "premium sites")),
    ("residential_farmhouse", ("farmhouse", "farm house", "weekend home")),
    ("residential_serviced", ("serviced apartment", "co-living", "coliving")),
    ("residential_rental", ("for rent", "flats on rent", "rental home",
                            "house for lease")),
    ("senior_living", ("senior living", "assisted living", "retirement home",
                       "retirement community")),
    ("commercial_coworking", ("coworking", "co-working", "managed workspace",
                              "managed office")),
    ("commercial_office", ("office space", "office tower", "grade a office",
                           "carpet area", "workspace", "commercial office",
                           "office for lease", "office for sale")),
    ("commercial_retail", ("retail space", "showroom", "shop for sale",
                           "shops for sale", "high street retail", "food court")),
    ("commercial_industrial", ("warehouse", "industrial park", "logistics park",
                               "industrial shed")),
    ("hospitality", ("hotel", "resort")),
    ("residential_apartment", ("apartment", "flat", "bhk", "high-rise",
                               "highrise", "high rise", "residences",
                               "condominium", "housing society", "possession",
                               "gated community")),
]
# Real-estate-ish but unmatched above -> other_real_estate, never unknown.
_REAL_ESTATE_HINTS = ("real estate", "realty", "property", "properties",
                      "homes", "builders")

_STAGE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("pre_launch", ("pre-launch", "pre launch", "prelaunch", "new launch",
                    "launching soon", "newly launched")),
    ("under_construction", ("under construction", "under-construction")),
    ("ready_to_move", ("ready to move", "ready-to-move", "possession ready",
                       "immediate possession", "ready possession")),
    ("resale", ("resale",)),
    ("rental", ("for rent", "on rent", "for lease")),
]


def classify_text(text: str) -> tuple[str, str]:
    """Map free text onto the taxonomy. Returns ``(category, evidence)`` where
    evidence is the keyword that decided it - ``("unknown", "")`` on no signal.
    Keyword-only by design: this normalizes analyst-written text, it never
    judges an ad (ads are classified by the essence analyst's vision pass)."""
    haystack = " " + re.sub(r"\s+", " ", (text or "").lower()) + " "
    if not haystack.strip():
        return "unknown", ""
    for category, keywords in _KEYWORD_RULES:
        for kw in keywords:
            if kw in haystack:
                return category, kw
    for kw in _REAL_ESTATE_HINTS:
        if kw in haystack:
            return "other_real_estate", kw
    return "unknown", ""


def classify_offering_stage(text: str) -> str:
    """Offering stage from free text; empty string when undeterminable."""
    haystack = (text or "").lower()
    for stage, keywords in _STAGE_RULES:
        if any(kw in haystack for kw in keywords):
            return stage
    return ""


def ensure_product_classified(product_data: dict) -> str:
    """Stage A, idempotent: return the product's effective category, deriving
    and stamping the classification fields onto ``product_data`` when absent or
    written under an older taxonomy. Mutates the live session dict; the
    campaign autosave persists the fields (business_storage).

    ``product_category_override`` wins unconditionally and skips derivation -
    the correction path when Stage A got it wrong (no re-fetch needed)."""
    override = (product_data.get("product_category_override") or "").strip()
    if override:
        if not product_data.get("product_market"):
            product_data["product_market"] = _derive_market(product_data)
        return override
    if (product_data.get("product_category")
            and product_data.get("taxonomy_version") == TAXONOMY_VERSION):
        return product_data["product_category"]

    signals = [
        ("businessType", product_data.get("business_type") or "", 0.9),
        ("productName", product_data.get("product_name") or "", 0.85),
        ("summary", product_data.get("summary") or "", 0.7),
        ("siteLinks", _site_links_text(product_data), 0.6),
    ]
    category, source, confidence = "unknown", "", 0.0
    for signal, text, conf in signals:
        got, _evidence = classify_text(text)
        if got != "unknown":
            category, source, confidence = got, signal, conf
            break
    product_data.update({
        "product_category": category,
        "product_subcategory": "",
        "product_market": _derive_market(product_data),
        "product_offering_stage": classify_offering_stage(
            " ".join(text for _, text, _ in signals[:3])),
        "product_category_source": source,
        "product_category_confidence": confidence,
        "taxonomy_version": TAXONOMY_VERSION,
    })
    return category


def _site_links_text(product_data: dict) -> str:
    """Host + path + anchor text of the site's links - they often name the
    asset class ("/villas-in-whitefield")."""
    parts: list[str] = []
    for link in product_data.get("site_links") or []:
        if isinstance(link, dict):
            parts.append(str(link.get("href") or "").replace("-", " "))
            parts.append(str(link.get("text") or ""))
    return " ".join(parts)


def _derive_market(product_data: dict) -> str:
    """The product's market string from its confirmed place - free text
    (map-pin label or address); matching is city-token based, not equality."""
    place = product_data.get("place") or {}
    return (place.get("display_name") or place.get("address") or "").strip()


# ── Stage C: the gate ─────────────────────────────────────────────────────

# One geography, one spelling: renamed Indian cities normalized before compare.
_CITY_ALIASES = {
    "bengaluru": "bangalore", "gurugram": "gurgaon", "bombay": "mumbai",
    "new delhi": "delhi", "mysuru": "mysore", "madras": "chennai",
    "calcutta": "kolkata", "puducherry": "pondicherry",
}


def _normalize_market(text: str) -> str:
    out = re.sub(r"[^a-z ]+", " ", (text or "").lower())
    out = re.sub(r"\s+", " ", out).strip()
    for alias, canonical in _CITY_ALIASES.items():
        out = out.replace(alias, canonical)
    return out


def market_matches(ad_market: str, product_market: str) -> bool:
    """City-level match: the ad's city (the part before '/') must appear in the
    product's market string, alias-normalized both ways. Locality mismatch is
    NOT checked here - per spec it's a flag, not an auto-reject. Either side
    empty = no evidence of mismatch = pass."""
    city = _normalize_market(ad_market.split("/")[0])
    product = _normalize_market(product_market)
    if not city or not product:
        return True
    return city in product or product in city


def gate_creative(
    product_category: str, product_market: str, essence,
) -> tuple[bool, str]:
    """Stage C verdict for one creative: ``(accept, rejection_reason)``.
    Fail closed - no essence, unknown, non-real-estate, a category mismatch,
    or sub-threshold confidence all reject. ``essence`` is the creative's
    ``Essence | None`` (kept untyped to stay import-leaf)."""
    if essence is None or not essence.category or essence.category == "unknown":
        return False, UNKNOWN_CATEGORY
    if essence.category == "other_industry":
        return False, NON_REAL_ESTATE
    if essence.category != product_category:
        return False, CATEGORY_MISMATCH
    if essence.category_confidence < ACCEPT_THRESHOLD:
        return False, LOW_CONFIDENCE
    if not market_matches(essence.market, product_market):
        return False, MARKET_MISMATCH
    return True, ""
