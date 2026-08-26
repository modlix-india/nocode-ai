"""Meta Targeting Adapter.

Provides category-specific, high-level methods for querying Meta's targeting
Graph API. Uses the `meta_client` singleton from `client.py` for all HTTP
calls. Each fetch method applies the correct multi-phase API strategy for
its category:

  - fetch_interests       : search per seed + targetingsuggestions expansion
  - fetch_behaviors       : full catalog browse + search per seed
  - fetch_demographics    : browse 5 fixed subtypes (no seeds)
  - search_open_demographics : targetingsearch per seed across 3 open subtypes
                               (work_positions, work_employers, education_majors)
  - validate              : batched GET targetingvalidation (50 per batch)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.agents.adzump.adapters.connections import fetch_meta_api_token
from app.agents.adzump.adapters.meta.client import meta_client
from app.agents.adzump.agents.meta_detailed_targeting.models import TargetingEntity
from app.agents.adzump.config import get_adzump_config

logger = logging.getLogger(__name__)

# Constants
META_API_TIMEOUT_SECONDS = 15.0
META_API_MAX_RETRY_ATTEMPTS = 2
META_API_SUGGESTIONS_MAX_SEEDS = 50
META_API_CONCURRENT_REQUESTS_LIMIT = 10

# Fixed catalog subtypes — small, enumerable lists. targetingbrowse returns all entries.
DEMOGRAPHIC_FIXED_SUBTYPES = (
    "life_events",
    "family_statuses",
    "income",
    "industries",
    "education_statuses",
)

# Open/searchable subtypes — large databases (job titles, employers, majors).
# targetingbrowse returns 0 entries; these require a keyword search like interests.
DEMOGRAPHIC_SEARCHABLE_SUBTYPES = (
    "work_positions",
    "work_employers",
    "education_majors",
)


# Helpers
async def _resolve_meta_token(client_code: str, auth_headers: dict[str, str]) -> str:
    """Resolve Meta access token: check local .env override first, then gateway."""
    local = get_adzump_config().meta.access_token
    if local:
        return local
    return await fetch_meta_api_token(client_code, auth_headers)


def _normalize_account_id(account_id: str) -> str:
    """Strip 'act_' prefix and sanitize ad account ID."""
    clean = str(account_id).strip().lower()
    if clean.startswith("act_"):
        return clean[4:]
    return clean


def _deduplicate(entities: list[TargetingEntity]) -> list[TargetingEntity]:
    """Remove duplicate entities by ID, preserving first-seen order."""
    seen: set[str] = set()
    result: list[TargetingEntity] = []
    for e in entities:
        if e.id and e.id not in seen:
            seen.add(e.id)
            result.append(e)
    return result


# Adapter
class TargetingAdapter:
    """Async adapter for Meta Detailed Targeting Graph API."""

    def __init__(self) -> None:
        self._semaphore: asyncio.Semaphore | None = None

    @property
    def semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(META_API_CONCURRENT_REQUESTS_LIMIT)
        return self._semaphore

    async def _fetch_with_retry(
        self,
        client_code: str,
        auth_headers: dict[str, str],
        account_id: str,
        endpoint: str,
        params: dict[str, Any],
        access_token: str | None = None,
    ) -> list[TargetingEntity]:
        """Execute a GET request with semaphore, timeout and exponential backoff.

        Only retries on transient errors: asyncio.TimeoutError, network errors
        (httpx.RequestError), and Meta 5xx/rate-limit responses (RuntimeError
        from _raise_for_meta_error with status >= 500 or 429).
        4xx client errors are not retried — they indicate a bad request that
        will produce the same result on retry.
        """
        clean_id = _normalize_account_id(account_id)
        full_endpoint = f"/act_{clean_id}/{endpoint}"

        for attempt in range(META_API_MAX_RETRY_ATTEMPTS):
            try:
                async with self.semaphore:
                    response = await asyncio.wait_for(
                        meta_client.get(
                            endpoint=full_endpoint,
                            client_code=client_code,
                            auth_headers=auth_headers,
                            params=params,
                            access_token=access_token,
                        ),
                        timeout=META_API_TIMEOUT_SECONDS,
                    )

                if "error" in response:
                    error_msg = response["error"].get("message") or str(response["error"])
                    error_code = response["error"].get("code", 0)
                    # Rate-limit (code 17 / 32 / 613) and server errors are retryable;
                    # auth and bad-request errors (code 190, 100, etc.) are not.
                    if error_code in (17, 32, 613):
                        raise RuntimeError(error_msg)  # will retry
                    raise ValueError(error_msg)  # 4xx-equivalent — will not retry

                data_list = response.get("data") or []
                entities = [TargetingEntity.from_meta(item) for item in data_list]
                return [e for e in entities if e is not None]

            except (asyncio.TimeoutError, OSError) as exc:
                # Transient network errors — always retry
                logger.warning(
                    "Meta API %s attempt %d/%d transient error: %s",
                    endpoint, attempt + 1, META_API_MAX_RETRY_ATTEMPTS, exc,
                )
                if attempt == META_API_MAX_RETRY_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(2 ** attempt)
            except RuntimeError as exc:
                # Rate-limit or explicit server error — retry
                logger.warning(
                    "Meta API %s attempt %d/%d retryable error: %s",
                    endpoint, attempt + 1, META_API_MAX_RETRY_ATTEMPTS, exc,
                )
                if attempt == META_API_MAX_RETRY_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(2 ** attempt)
            except Exception as exc:
                # Non-retryable: 4xx, ValueError from bad request, etc.
                logger.warning(
                    "Meta API %s non-retryable error: %s",
                    endpoint, exc,
                )
                raise

        return []

    # search
    async def search(
        self,
        client_code: str,
        auth_headers: dict[str, str],
        account_id: str,
        q: str,
    ) -> list[TargetingEntity]:
        """Perform a general targeting search across interests, behaviors, and demographics."""
        token = await _resolve_meta_token(client_code, auth_headers)
        return await self._fetch_with_retry(
            client_code=client_code,
            auth_headers=auth_headers,
            account_id=account_id,
            endpoint="targetingsearch",
            params={"q": q},
            access_token=token,
        )

    # fetch_interests
    async def fetch_interests(
        self,
        client_code: str,
        auth_headers: dict[str, str],
        account_id: str,
        seeds: list[str],
    ) -> list[TargetingEntity]:
        """Discover interests via two-phase: keyword search + recommendation expansion.

        Phase 1 - Parallel keyword search per seed:
            GET /{account_id}/targetingsearch?q={seed}&limit_type=interests

        Phase 2 - Recommendation expansion (batched by 10 seed IDs):
            GET /{account_id}/targetingsuggestions
                ?targeting_list=[{"type":"interests","id":"..."}]&limit_type=interests

        Returns deduplicated interest entities from both phases.
        """
        token = await _resolve_meta_token(client_code, auth_headers)

        # Phase 1: parallel search per seed — limit=50 to override Meta's 8-result default
        search_tasks = [
            self._fetch_with_retry(
                client_code=client_code,
                auth_headers=auth_headers,
                account_id=account_id,
                endpoint="targetingsearch",
                params={"q": seed, "limit_type": "interests", "limit": 50},
                access_token=token,
            )
            for seed in seeds
        ]
        search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

        unique_map: dict[str, TargetingEntity] = {}
        for result in search_results:
            if isinstance(result, list):
                for entity in result:
                    if entity.id and entity.id not in unique_map:
                        unique_map[entity.id] = entity

        # Phase 2: recommendation expansion batched by 10
        seed_ids = list(unique_map.keys())[:META_API_SUGGESTIONS_MAX_SEEDS]
        if not seed_ids:
            return list(unique_map.values())

        batch_size = 10
        id_batches = [seed_ids[i: i + batch_size] for i in range(0, len(seed_ids), batch_size)]

        suggestion_tasks = [
            self._fetch_with_retry(
                client_code=client_code,
                auth_headers=auth_headers,
                account_id=account_id,
                endpoint="targetingsuggestions",
                params={
                    "targeting_list": json.dumps(
                        [{"type": "interests", "id": sid} for sid in batch]
                    ),
                    "limit_type": "interests",
                },
                access_token=token,
            )
            for batch in id_batches
        ]
        suggestion_results = await asyncio.gather(*suggestion_tasks, return_exceptions=True)

        all_entities = list(unique_map.values())
        for result in suggestion_results:
            if isinstance(result, list):
                all_entities.extend(result)

        return _deduplicate(all_entities)

    # fetch_behaviors
    async def fetch_behaviors(
        self,
        client_code: str,
        auth_headers: dict[str, str],
        account_id: str,
        seeds: list[str],
    ) -> list[TargetingEntity]:
        """Fetch behaviors via full catalog browse + per-seed keyword search.

        Parallel:
            GET /{account_id}/targetingbrowse?limit_type=behaviors
            GET /{account_id}/targetingsearch?q={seed}&limit_type=behaviors  (per seed)

        Returns merged, deduplicated behavior entities.
        """
        token = await _resolve_meta_token(client_code, auth_headers)

        browse_task = self._fetch_with_retry(
            client_code=client_code,
            auth_headers=auth_headers,
            account_id=account_id,
            endpoint="targetingbrowse",
            params={"limit_type": "behaviors"},
            access_token=token,
        )
        search_tasks = [
            self._fetch_with_retry(
                client_code=client_code,
                auth_headers=auth_headers,
                account_id=account_id,
                endpoint="targetingsearch",
                params={"q": seed, "limit_type": "behaviors", "limit": 50},
                access_token=token,
            )
            for seed in seeds
        ]

        all_results = await asyncio.gather(*search_tasks, browse_task, return_exceptions=True)

        entities: list[TargetingEntity] = []
        for result in all_results:
            if isinstance(result, list):
                entities.extend(result)

        return _deduplicate(entities)

    # fetch_demographics  (fixed catalog — no seeds needed)
    async def fetch_demographics(
        self,
        client_code: str,
        auth_headers: dict[str, str],
        account_id: str,
    ) -> list[TargetingEntity]:
        """Fetch the complete fixed demographic catalog via per-subtype browse.

        Browses only the 5 fixed subtypes (life_events, family_statuses, income,
        industries, education_statuses) which return a full, enumerable list.
        The 3 open subtypes (work_positions, work_employers, education_majors)
        are NOT browsed here because they return 0 results without a query.
        Use search_open_demographics() for those.

        Parallel:
            GET /{account_id}/targetingbrowse?limit_type={subtype}
                for each of DEMOGRAPHIC_FIXED_SUBTYPES
        Returns merged, deduplicated demographic entities (~99 total).
        """
        token = await _resolve_meta_token(client_code, auth_headers)

        browse_tasks = [
            self._fetch_with_retry(
                client_code=client_code,
                auth_headers=auth_headers,
                account_id=account_id,
                endpoint="targetingbrowse",
                params={"limit_type": subtype},
                access_token=token,
            )
            for subtype in DEMOGRAPHIC_FIXED_SUBTYPES
        ]

        all_results = await asyncio.gather(*browse_tasks, return_exceptions=True)

        entities: list[TargetingEntity] = []
        for result in all_results:
            if isinstance(result, list):
                entities.extend(result)

        return _deduplicate(entities)

    # search_open_demographics  (job titles / employers / majors)
    async def search_open_demographics(
        self,
        client_code: str,
        auth_headers: dict[str, str],
        account_id: str,
        seeds: list[str],
    ) -> list[TargetingEntity]:
        """Search open demographic databases (work_positions, work_employers, education_majors).

        These subtypes contain thousands of entries and cannot be enumerated via browse.
        They require keyword-based search, identical to fetch_interests.

        Parallel:
            GET /{account_id}/targetingsearch?q={seed}&limit_type={subtype}
                for each seed x each of DEMOGRAPHIC_SEARCHABLE_SUBTYPES
        Returns merged, deduplicated entities.
        """
        token = await _resolve_meta_token(client_code, auth_headers)

        search_tasks = [
            self._fetch_with_retry(
                client_code=client_code,
                auth_headers=auth_headers,
                account_id=account_id,
                endpoint="targetingsearch",
                params={"q": seed, "limit_type": subtype, "limit": 50},
                access_token=token,
            )
            for seed in seeds
            for subtype in DEMOGRAPHIC_SEARCHABLE_SUBTYPES
        ]

        all_results = await asyncio.gather(*search_tasks, return_exceptions=True)

        entities: list[TargetingEntity] = []
        for result in all_results:
            if isinstance(result, list):
                entities.extend(result)

        return _deduplicate(entities)

    # validate
    async def validate(
        self,
        client_code: str,
        auth_headers: dict[str, str],
        account_id: str,
        entities: list[TargetingEntity],
    ) -> list[TargetingEntity]:
        """Verify targeting entities are still active in Meta's system.

        Batches by 50, uses GET targetingvalidation with targeting_list query param.
        Returns only active entities confirmed by Meta. Invalid, deprecated, or
        failed items are rejected.
        """
        if not entities:
            return []

        token = await _resolve_meta_token(client_code, auth_headers)
        clean_id = _normalize_account_id(account_id)
        full_endpoint = f"/act_{clean_id}/targetingvalidation"
        batch_size = 50
        batches = [entities[i: i + batch_size] for i in range(0, len(entities), batch_size)]

        async def _validate_batch(batch: list[TargetingEntity]) -> list[TargetingEntity]:
            try:
                targeting_list = [e.to_validation_pair() for e in batch if e.id]
                if not targeting_list:
                    return []
                response = await meta_client.get(
                    endpoint=full_endpoint,
                    client_code=client_code,
                    auth_headers=auth_headers,
                    params={"targeting_list": json.dumps(targeting_list)},
                    access_token=token,
                )
                if "error" in response:
                    logger.warning("targetingvalidation error response: %s", response.get("error"))
                    return []

                # Parse Meta Graph API's validated items and enrich entities with fresh metadata
                data_items = response.get("data", [])
                meta_item_map = {
                    str(item.get("id")).strip(): item
                    for item in data_items
                    if item.get("valid") is True and item.get("id") is not None
                }

                validated: list[TargetingEntity] = []
                for e in batch:
                    e_id = str(e.id).strip()
                    if e_id in meta_item_map:
                        item_data = meta_item_map[e_id]
                        enriched = TargetingEntity.from_meta(item_data)
                        if enriched:
                            # Preserve original type if Meta returned a generic type
                            if not enriched.type or enriched.type == "interests":
                                if e.type and e.type != "interests":
                                    enriched.type = e.type
                            validated.append(enriched)
                        else:
                            validated.append(e)

                return validated
            except Exception as exc:
                logger.warning("Validation batch exception: %s", exc)
                return []

        results = await asyncio.gather(*[_validate_batch(b) for b in batches])
        return [entity for batch_result in results for entity in batch_result]
