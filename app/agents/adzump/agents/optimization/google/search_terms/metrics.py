from __future__ import annotations

from structlog import get_logger

logger = get_logger(__name__)

DEFAULT_COST_PER_CONVERSION_THRESHOLD = 1000.0


def evaluate_cost_per_conversion(
    term_data: dict,
    threshold: float = DEFAULT_COST_PER_CONVERSION_THRESHOLD,
) -> dict | None:
    """
    Evaluate search term performance using cost per conversion.

    Returns normalized evaluation payload or None.
    """

    status = str(term_data.get("status") or "").strip().upper()

    if status != "NONE":
        return None

    metrics = term_data.get("metrics", {})

    raw_value = metrics.get("cost_per_conversion")

    if raw_value is None:
        return None

    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid cost per conversion",
            value=raw_value,
        )
        return None

    base_result = {
        "text": term_data.get("search_term"),
        "status": status,
        "match_type": term_data.get("match_type"),
        "ad_group_id": term_data.get("ad_group_id"),
        "ad_group_name": term_data.get("ad_group_name"),
        "metrics": metrics,
    }

    if value < threshold:
        return {
            **base_result,
            "recommendation_type": "positive",
            "reason": (f"Cost per conversion is {value} (below threshold {threshold})"),
        }

    return {
        **base_result,
        "recommendation_type": "negative",
        "reason": (f"Cost per conversion is {value} (above threshold {threshold})"),
    }


def merge_duplicate_terms(search_terms: list[dict]) -> list[dict]:
    """
    Merge duplicate search terms by:
    - search term
    - match type
    """

    merged_terms: dict[tuple[str, str], dict] = {}

    for term in search_terms:
        term_text = term.get("search_term", "").strip().lower()

        match_type = term.get("match_type", "").strip().upper()

        key = (term_text, match_type)

        if key not in merged_terms:
            merged_terms[key] = term
            continue

        existing = merged_terms[key]

        existing_metrics = existing.get("metrics", {})
        new_metrics = term.get("metrics", {})

        for metric_key in [
            "impressions",
            "clicks",
            "conversions",
            "cost",
        ]:
            existing_metrics[metric_key] = existing_metrics.get(
                metric_key, 0
            ) + new_metrics.get(metric_key, 0)

        for metric_key in [
            "ctr",
            "average_cpc",
            "cost_per_conversion",
        ]:
            existing_metrics[metric_key] = (
                existing_metrics.get(metric_key, 0) + new_metrics.get(metric_key, 0)
            ) / 2

        existing["metrics"] = existing_metrics

    return list(merged_terms.values())


async def analyze_search_term_metrics(
    search_terms: list[dict],
) -> list[dict]:
    """
    Analyze normalized search terms using metric evaluators.
    """

    merged_terms = merge_duplicate_terms(search_terms)

    results: list[dict] = []

    for term in merged_terms:
        evaluation = evaluate_cost_per_conversion(term)

        if evaluation:
            results.append(evaluation)

    logger.info(
        "Search term metric analysis completed",
        total=len(results),
    )

    return results
