"""Keyword Scorer.

Scores and ranks Google Ads Keyword Planner suggestions using semantic similarity,
competition metrics, search volume, and business relevance.
"""

import asyncio
from math import inf
from typing import Dict, List, Optional, Tuple, Any

import logging

from app.services.llm_provider import get_llm_provider


async def generate_embeddings(texts: List[str]) -> List[List[float]]:
    """Central helper to generate embeddings via LLMProvider."""
    return await get_llm_provider("openai").generate_embeddings(texts)


logger = logging.getLogger(__name__)

SCORE_WEIGHTS = {
    "volume": 0.22,
    "competition": 0.18,
    "business_relevance": 0.23,
    "intent": 0.14,
    "semantic": 0.13,
    "native_boost": 0.10,
}

VOLUME_SCORE_TIERS: List[Tuple[int, float, int]] = [
    (0, 100, 20),
    (100, 500, 40),
    (500, 1000, 60),
    (1000, 5000, 80),
    (5000, inf, 100),
]

BUSINESS_RELEVANCE_SCORES = {"high": 100, "medium": 60, "low": 20}

INTENT_TYPE_SCORES = {
    "transactional": 100,
    "commercial": 80,
    "navigational": 60,
    "informational": 40,
    "unknown": 20,
}

CROSS_BUSINESS_PENALTY = 0.5
MINIMUM_SCORE = 40

# Soft-cap: 50 conversions uplift maps to a native_boost score of 100.
NATIVE_UPLIFT_CAP = 50

# Flat bonus for native recs that have no impact data — Google still saw
# enough signal in the account's auction history to suggest the keyword.
NATIVE_FLAT_BONUS = 60

# Fallback defaults when keyword data is missing
FALLBACK_COMPETITION_INDEX = 0.5
COMPETITION_SCORE_MULTIPLIER = 100
FALLBACK_BUSINESS_RELEVANCE = "medium"
FALLBACK_BUSINESS_SCORE = 60
FALLBACK_INTENT = "unknown"
FALLBACK_INTENT_SCORE = 20
DEFAULT_SEMANTIC_SCORE = 50.0
FALLBACK_VOLUME_SCORE = 20.0

# Score precision
SCORE_DECIMALS = 2

# Embedding similarity initialisation
INITIAL_BEST_SCORE = -1.0

# Normalise similarity/uplift to 0-100 percentage scale
PERCENTAGE_MULTIPLIER = 100

# Native boost max score cap
NATIVE_BOOST_MAX_SCORE = 100

# Fallback for volume / conversions when the upstream payload has no data
FALLBACK_VOLUME = 0
FALLBACK_CONVERSIONS = 0


async def assign_ad_groups(
    new_keywords: List[str], existing_keywords: List[Dict[str, Any]]
) -> Dict[str, Dict[str, str]]:
    """Assign each suggestion to the ad group with highest embedding similarity."""
    ad_groups: Dict[str, Dict[str, Any]] = {}
    for e in existing_keywords:
        ag_id = e.get("ad_group_id", "")
        if ag_id not in ad_groups:
            ad_groups[ag_id] = {
                "ad_group_id": ag_id,
                "ad_group_name": e.get("ad_group_name", ""),
                "keywords": [],
            }
        ad_groups[ag_id]["keywords"].append(e["keyword"])

    if not ad_groups or not new_keywords:
        return {}

    if len(ad_groups) == 1:
        ag = next(iter(ad_groups.values()))
        return {
            t.lower(): {
                "ad_group_id": ag["ad_group_id"],
                "ad_group_name": ag["ad_group_name"],
            }
            for t in new_keywords
        }

    try:
        all_existing = [kw for ag in ad_groups.values() for kw in ag["keywords"]]
        new_emb, existing_emb = await asyncio.gather(
            generate_embeddings(new_keywords),
            generate_embeddings(all_existing),
        )

        assignments: Dict[str, Dict[str, str]] = {}
        for i, text in enumerate(new_keywords):
            v_new = new_emb[i]
            best_id, best_score = "", INITIAL_BEST_SCORE

            # Group existing embeddings by their ad_group indices to find the closest match
            offset = 0
            for ag_id, ag_data in ad_groups.items():
                count = len(ag_data["keywords"])
                group_embs = existing_emb[offset : offset + count]
                offset += count

                if not group_embs:
                    continue

                # Compute maximum similarity in this ad group using pure Python dot product
                group_max = max(
                    sum(x * y for x, y in zip(v_new, v_exist)) for v_exist in group_embs
                )
                if group_max > best_score:
                    best_score = group_max
                    best_id = ag_id

            if best_id:
                ag = ad_groups[best_id]
                assignments[text.lower()] = {
                    "ad_group_id": ag["ad_group_id"],
                    "ad_group_name": ag["ad_group_name"],
                }
        return assignments
    except Exception:
        logger.warning("ad_group_assignment_failed", exc_info=True)
        return {}


async def calculate_semantic_scores(
    suggestion_texts: List[str], anchor_texts: List[str]
) -> Dict[str, float]:
    """Compute max cosine similarity of each suggestion against all anchors."""
    if not suggestion_texts or not anchor_texts:
        return {}
    try:
        anchor_embeddings, suggestion_embeddings = await asyncio.gather(
            generate_embeddings(anchor_texts),
            generate_embeddings(suggestion_texts),
        )

        def _cosine(a: List[float], b: List[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            norm_a = sum(x * x for x in a) ** 0.5
            norm_b = sum(x * x for x in b) ** 0.5
            denom = norm_a * norm_b
            return dot / denom if denom > 0 else 0.0

        scores: Dict[str, float] = {}
        for i, kw in enumerate(suggestion_texts):
            v_sug = suggestion_embeddings[i]
            max_sim = INITIAL_BEST_SCORE
            for v_anc in anchor_embeddings:
                sim = _cosine(v_sug, v_anc)
                if sim > max_sim:
                    max_sim = sim
            scores[kw.strip().lower()] = round(
                float(max_sim) * PERCENTAGE_MULTIPLIER, SCORE_DECIMALS
            )
        return scores
    except Exception:
        logger.error(
            "semantic_scoring_failed suggestions=%s anchors=%s",
            suggestion_texts,
            anchor_texts,
            exc_info=True,
        )
        return {}


def score_and_rank_keywords(keywords: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Score, filter below MINIMUM_SCORE, and sort descending by final_score."""
    scored = []
    for kw in keywords:
        result = _score_single_keyword(kw)
        if result is not None:
            scored.append(result)
    scored.sort(key=lambda k: k["final_score"], reverse=True)
    return scored


def _score_single_keyword(kw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    volume_score = _volume_score(kw.get("volume", FALLBACK_VOLUME))
    competition_score = (
        1 - kw.get("competitionIndex", FALLBACK_COMPETITION_INDEX)
    ) * COMPETITION_SCORE_MULTIPLIER
    business_score = BUSINESS_RELEVANCE_SCORES.get(
        kw.get("business_relevance", FALLBACK_BUSINESS_RELEVANCE),
        FALLBACK_BUSINESS_SCORE,
    )
    intent_score = INTENT_TYPE_SCORES.get(
        kw.get("intent", FALLBACK_INTENT), FALLBACK_INTENT_SCORE
    )
    semantic_score = kw.get("semantic_score", DEFAULT_SEMANTIC_SCORE)

    # Native recommendation boost: use Google's predicted conversion uplift
    # normalised to 0-100. Falls back to a flat bonus for any native rec
    # with no impact data (still better than a non-native keyword at 0).
    native_boost = 0.0
    if kw.get("is_native_recommendation"):
        impact = kw.get("impact", {})
        base_conv = impact.get("base", {}).get("conversions") or FALLBACK_CONVERSIONS
        pot_conv = (
            impact.get("potential", {}).get("conversions") or FALLBACK_CONVERSIONS
        )
        uplift = pot_conv - base_conv
        if uplift > FALLBACK_CONVERSIONS:
            native_boost = min(
                uplift / NATIVE_UPLIFT_CAP * PERCENTAGE_MULTIPLIER,
                NATIVE_BOOST_MAX_SCORE,
            )
        else:
            native_boost = NATIVE_FLAT_BONUS

    final = (
        volume_score * SCORE_WEIGHTS["volume"]
        + competition_score * SCORE_WEIGHTS["competition"]
        + business_score * SCORE_WEIGHTS["business_relevance"]
        + intent_score * SCORE_WEIGHTS["intent"]
        + semantic_score * SCORE_WEIGHTS["semantic"]
        + native_boost * SCORE_WEIGHTS["native_boost"]
    )

    if kw.get("is_cross_business"):
        final *= CROSS_BUSINESS_PENALTY

    if final < MINIMUM_SCORE:
        return None

    return {
        **kw,
        "final_score": round(final, SCORE_DECIMALS),
        "score_breakdown": {
            "volume": round(volume_score, SCORE_DECIMALS),
            "competition": round(competition_score, SCORE_DECIMALS),
            "business_relevance": round(business_score, SCORE_DECIMALS),
            "intent": round(intent_score, SCORE_DECIMALS),
            "semantic": round(semantic_score, SCORE_DECIMALS),
            "native_boost": round(native_boost, SCORE_DECIMALS),
        },
    }


def _volume_score(volume: int) -> float:
    for low, high, score in VOLUME_SCORE_TIERS:
        if low <= volume < high:
            return score
    return FALLBACK_VOLUME_SCORE
