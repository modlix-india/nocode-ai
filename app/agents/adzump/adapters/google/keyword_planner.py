"""Google Ads Keyword Planner adapter — generateKeywordIdeas.

The official source of Google search volume, competition, and CPC bid estimates,
and the relevance gate for candidate keywords (terms with no Google demand drop
out). Seeds can be keywords and/or the business URL; the Planner both *scores* the
seeds and *expands* them with related Google ideas.

REST returns camelCase; bid values are micros (divided by 1e6). Default network is
GOOGLE_SEARCH_AND_PARTNERS (Search campaigns commonly include partners; gives the
fuller volume picture) — overridable per call.
Source: https://developers.google.com/google-ads/api/docs/keyword-planning/generate-keyword-ideas
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.agents.adzump.adapters.google.client import google_ads_client

logger = logging.getLogger(__name__)

# API constants
_MICROS_PER_UNIT = 1_000_000  # Google money fields are in micros
_COMPETITION_INDEX_MAX = 100.0  # API competition_index is 0-100; we expose 0-1
_CURRENCY_DECIMALS = 2

DEFAULT_LANGUAGE = "languageConstants/1000"  # English
INDIA_GEO_TARGET = "geoTargetConstants/2356"  # fallback when no location resolves
DEFAULT_NETWORK = "GOOGLE_SEARCH_AND_PARTNERS"  # proven default (see module docstring)
_VALID_COMPETITION_LEVELS = {"LOW", "MEDIUM", "HIGH"}

# Request tuning (not hard API limits; documented choices)
_SEED_CHUNK_SIZE = 15  # proven batch size; smaller chunks expand more focused
_CHUNK_CONCURRENCY = 3  # bounded — faster than sequential, rate-safe per request
_MAX_RETRIES = 2
_RETRY_BACKOFF_BASE_SECONDS = 0.5  # exponential: base * 2**attempt (0.5s, then 1.0s)
_LOG_ERROR_MAXLEN = 160  # truncation for error messages in logs

# Circuit breaker — N consecutive failures open it for a cooldown (fail fast, shared state).
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_SECONDS = 30.0
_breaker_failures = 0
_breaker_open_until = 0.0


class PlannerUnavailable(RuntimeError):
    """Raised when the breaker is open (recent repeated failures) — caller fails soft."""


def _breaker_check() -> None:
    if _breaker_open_until > time.monotonic():
        raise PlannerUnavailable(
            "Keyword Planner temporarily unavailable (circuit open)."
        )


def _breaker_record(*, ok: bool) -> None:
    global _breaker_failures, _breaker_open_until
    if ok:
        _breaker_failures = 0
        _breaker_open_until = 0.0
        return
    _breaker_failures += 1
    if _breaker_failures >= _BREAKER_THRESHOLD:
        _breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECONDS
        logger.warning(
            "keyword_planner circuit OPEN after %d consecutive failures; cooling down %.0fs",
            _breaker_failures,
            _BREAKER_COOLDOWN_SECONDS,
        )


def _micros_to_currency(value: object) -> float:
    try:
        return round(int(value) / _MICROS_PER_UNIT, _CURRENCY_DECIMALS)
    except (TypeError, ValueError):
        return 0.0


def _build_payload(
    chunk: list[str], url: str | None, geo: list[str], language: str, network: str
) -> dict:
    payload: dict = {
        "language": language,
        "geoTargetConstants": list(geo),
        "keywordPlanNetwork": network,
        "includeAdultKeywords": False,
    }
    if url and url.strip():
        payload["keywordAndUrlSeed"] = {"url": url.strip(), "keywords": chunk}
    else:
        payload["keywordSeed"] = {"keywords": chunk}
    return payload


def _parse_idea(idea: dict, metrics_key: str = "keywordIdeaMetrics") -> dict | None:
    """Normalise one Planner result into our metric dict; ``None`` if it has no text.
    ``metrics_key`` differs by endpoint: generateKeywordIdeas nests metrics under
    ``keywordIdeaMetrics``, generateKeywordHistoricalMetrics under ``keywordMetrics``.
    Policy filters (length, word count, category) live in the service layer."""
    text = (idea.get("text") or "").strip().lower()
    if not text:
        return None
    metrics = idea.get(metrics_key) or {}
    competition = metrics.get("competition", "UNKNOWN")
    return {
        "keyword": text,
        "volume": int(metrics.get("avgMonthlySearches") or 0),
        "competition": competition
        if competition in _VALID_COMPETITION_LEVELS
        else "UNKNOWN",
        "competition_index": float(metrics.get("competitionIndex") or 0)
        / _COMPETITION_INDEX_MAX,
        # CPC top-of-page bid range — the commercial-value signal.
        "cpc_low": _micros_to_currency(metrics.get("lowTopOfPageBidMicros")),
        "cpc_high": _micros_to_currency(metrics.get("highTopOfPageBidMicros")),
    }


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def _post_with_retry(
    endpoint: str,
    payload: dict,
    client_code: str,
    auth_headers: dict,
    login_customer_id: str,
) -> dict:
    _breaker_check()  # fail fast if the breaker is open (recent repeated failures)
    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = await google_ads_client.post(
                endpoint,
                payload,
                client_code,
                auth_headers,
                login_customer_id=login_customer_id,
            )
            _breaker_record(ok=True)
            return response
        except Exception as exc:
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(_RETRY_BACKOFF_BASE_SECONDS * (2**attempt))
                continue
            _breaker_record(ok=False)
            raise exc


async def fetch_keyword_ideas(
    seeds: list[str],
    *,
    customer_id: str,
    login_customer_id: str,
    client_code: str,
    auth_headers: dict,
    url: str | None = None,
    geo_target_constants: list[str] | None = None,
    language: str = DEFAULT_LANGUAGE,
    network: str = DEFAULT_NETWORK,
) -> list[dict]:
    """Return Google keyword ideas (volume / competition / CPC) for ``seeds``.

    Seeds are batched and fetched with bounded concurrency; each batch also passes
    the business ``url`` (keywordAndUrlSeed) when given, so Google reads the landing
    page for richer ideas. **Fail-soft per batch** — a failed batch is logged and
    skipped, not fatal. Returns ideas de-duplicated (highest-volume occurrence kept),
    sorted by volume descending.
    """
    seeds = [s.strip() for s in seeds if s and s.strip()]
    if not seeds:
        return []
    geo = geo_target_constants or [INDIA_GEO_TARGET]
    endpoint = f"customers/{customer_id}:generateKeywordIdeas"
    semaphore = asyncio.Semaphore(_CHUNK_CONCURRENCY)

    async def _fetch_chunk(chunk: list[str]) -> list[dict]:
        async with semaphore:
            payload = _build_payload(chunk, url, geo, language, network)
            try:
                response = await _post_with_retry(
                    endpoint, payload, client_code, auth_headers, login_customer_id
                )
                return [
                    idea
                    for result in response.get("results", [])
                    if (idea := _parse_idea(result)) is not None
                ]
            except Exception as exc:
                logger.warning(
                    "keyword_planner: chunk failed (%d seeds) after retries: %s",
                    len(chunk),
                    str(exc)[:_LOG_ERROR_MAXLEN],
                )
                return []

    groups = await asyncio.gather(
        *(_fetch_chunk(chunk) for chunk in _chunks(seeds, _SEED_CHUNK_SIZE))
    )

    # Dedup keeping the highest-volume occurrence of each keyword.
    best: dict[str, dict] = {}
    for group in groups:
        for idea in group:
            keyword = idea["keyword"]
            if keyword not in best or idea["volume"] > best[keyword]["volume"]:
                best[keyword] = idea
    return sorted(best.values(), key=lambda idea: idea["volume"], reverse=True)


async def fetch_keyword_historical_metrics(
    keywords: list[str],
    *,
    customer_id: str,
    login_customer_id: str,
    client_code: str,
    auth_headers: dict,
    geo_target_constants: list[str] | None = None,
    language: str = DEFAULT_LANGUAGE,
    network: str = DEFAULT_NETWORK,
) -> list[dict]:
    """Return volume / competition / CPC for an EXACT set of keywords.

    Unlike fetch_keyword_ideas (which expands seeds), this scores the keywords as
    given — used for terms outside the ideas pool (negatives) and for keywords the
    user adds in the review panel. Fail-soft: returns [] on error.
    """
    keywords = [k.strip() for k in keywords if k and k.strip()]
    if not keywords:
        return []
    geo = geo_target_constants or [INDIA_GEO_TARGET]
    endpoint = f"customers/{customer_id}:generateKeywordHistoricalMetrics"
    payload = {
        "keywords": keywords,
        "language": language,
        "geoTargetConstants": list(geo),
        "keywordPlanNetwork": network,
        "includeAdultKeywords": False,
    }
    try:
        response = await _post_with_retry(
            endpoint, payload, client_code, auth_headers, login_customer_id
        )
    except Exception as exc:
        logger.warning(
            "historical_metrics failed (%d keywords): %s",
            len(keywords),
            str(exc)[:_LOG_ERROR_MAXLEN],
        )
        return []

    out: list[dict] = []
    for result in response.get("results", []):
        idea = _parse_idea(result, metrics_key="keywordMetrics")
        if idea is not None:
            out.append(idea)
    return out
