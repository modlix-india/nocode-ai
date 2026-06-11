import logging
import httpx
import asyncio
from typing import List

logger = logging.getLogger(__name__)

AUTOCOMPLETE_URL = "http://suggestqueries.google.com/complete/search"


def get_httpx_client() -> httpx.AsyncClient:
    """Simple wrapper for httpx client."""
    return httpx.AsyncClient()


async def fetch_autocomplete_suggestions(
    seed_keyword: str,
    max_results: int = 5,
    language: str = "en",
    client: httpx.AsyncClient | None = None,
) -> List[str]:
    """Fetch keyword suggestions from Google Autocomplete API."""
    try:
        params = {
            "client": "firefox",
            "q": seed_keyword,
            "hl": language,
        }

        should_close = False
        if client is None:
            client = get_httpx_client()
            should_close = True

        try:
            response = await client.get(AUTOCOMPLETE_URL, params=params, timeout=5.0)
            response.raise_for_status()

            # Response format: [query, [suggestions], ...]
            data = response.json()
            suggestions = data[1] if len(data) > 1 else []

            # Limit results
            limited_suggestions = suggestions[:max_results]

            logger.debug(
                "autocomplete_suggestions_fetched seed=%s count=%d",
                seed_keyword,
                len(limited_suggestions),
            )

            return limited_suggestions
        finally:
            if should_close:
                await client.aclose()

    except Exception as e:
        logger.warning(
            "autocomplete_fetch_failed seed=%s error=%s", seed_keyword, str(e)
        )
        return []


async def batch_fetch_autocomplete_suggestions(
    seed_keywords: List[str],
    max_results_per_seed: int = 5,
    language: str = "en",
) -> List[str]:
    """Fetch autocomplete suggestions for multiple seed keywords in parallel."""
    logger.info("autocomplete_batch_started seeds=%d", len(seed_keywords))

    semaphore = asyncio.Semaphore(10)

    async def _fetch_one(seed: str, c: httpx.AsyncClient) -> list:
        async with semaphore:
            return await fetch_autocomplete_suggestions(
                seed, max_results_per_seed, language, client=c
            )

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *[_fetch_one(seed, client) for seed in seed_keywords],
            return_exceptions=True,
        )

    # Flatten and deduplicate
    all_suggestions = []
    for result in results:
        if isinstance(result, list):
            all_suggestions.extend(result)
        elif isinstance(result, Exception):
            logger.warning("Individual seed expansion failed: %s", str(result))

    # Remove duplicates while preserving order
    seen = set()
    unique_suggestions = []
    for suggestion in all_suggestions:
        suggestion_lower = suggestion.lower().strip()
        if suggestion_lower and suggestion_lower not in seen:
            seen.add(suggestion_lower)
            unique_suggestions.append(suggestion)

    logger.info(
        "autocomplete_expansion_complete total=%d seeds=%d",
        len(unique_suggestions),
        len(seed_keywords),
    )

    return unique_suggestions
