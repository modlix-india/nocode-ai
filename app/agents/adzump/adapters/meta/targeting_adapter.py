"""Meta Targeting Adapter.

Provides category-specific, high-level methods for querying Meta's targeting
Graph API. Uses the `meta_client` singleton from `client.py` for all HTTP
calls. Each fetch method applies the correct multi-phase API strategy for
its category:

  - fetch_interests       : search per seed + targetingsuggestions expansion
  - fetch_behaviors       : full catalog browse + search per seed
  - fetch_demographics    : browse 5 fixed subtypes + optional search 3 open subtypes
  - search_open_demographics : targetingsearch per seed across work/education subtypes
  - validate              : batched GET targetingvalidation (50 per batch)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.agents.adzump.adapters.meta.client import meta_client
from app.agents.adzump.agents.meta_detailed_targeting.models import TargetingEntity

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
META_API_TIMEOUT_SECONDS = 15.0
META_API_MAX_RETRY_ATTEMPTS = 2
META_API_SUGGESTIONS_MAX_SEEDS = 50
META_API_CONCURRENT_REQUESTS_SEMAPHORE = asyncio.Semaphore(10)

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
    "moms",                  
    "home_ownership",       
    "household_composition",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _normalize_account_id(account_id: str) -> str:
    """Strip 'act_' prefix and sanitize ad account ID."""
    clean = str(account_id).strip().lower()
    if clean.startswith("act_"):
        return clean[4:]
    return clean



def _parse_entity(item: dict[str, Any]) -> TargetingEntity | None:
    """Parse a raw Meta API item into a TargetingEntity. Returns None on failure."""
    return TargetingEntity.from_meta(item)


def _deduplicate(entities: list[TargetingEntity]) -> list[TargetingEntity]:
    """Remove duplicate entities by ID, preserving first-seen order."""
    seen: set[str] = set()
    result: list[TargetingEntity] = []
    for entity in entities:
        entity_id = str(entity.id) if entity.id else None
        if not entity_id or entity_id in seen:
            continue
        seen.add(entity_id)
        result.append(entity)
    return result


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------
class TargetingAdapter:
    """Category-aware adapter for Meta Ads Graph API targeting endpoints."""

    async def _fetch_with_retry(
        self,
        client_code: str,
        auth_headers: dict[str, str],
        account_id: str,
        endpoint: str,
        params: dict[str, Any],
    ) -> list[TargetingEntity]:
        """Execute a GET request with semaphore, timeout and exponential backoff."""
        clean_id = _normalize_account_id(account_id)
        full_endpoint = f"/act_{clean_id}/{endpoint}"

        for attempt in range(META_API_MAX_RETRY_ATTEMPTS):
            try:
                async with META_API_CONCURRENT_REQUESTS_SEMAPHORE:
                    response = await asyncio.wait_for(
                        meta_client.get(
                            endpoint=full_endpoint,
                            client_code=client_code,
                            auth_headers=auth_headers,
                            params=params,
                        ),
                        timeout=META_API_TIMEOUT_SECONDS,
                    )

                if "error" in response:
                    error_msg = response["error"].get("message") or str(response["error"])
                    raise RuntimeError(error_msg)

                data_list = response.get("data") or []
                entities = [_parse_entity(item) for item in data_list]
                return [e for e in entities if e is not None]

            except Exception as exc:
                logger.warning(
                    "Meta API %s attempt %d/%d failed: %s",
                    endpoint, attempt + 1, META_API_MAX_RETRY_ATTEMPTS, exc,
                )
                if attempt == META_API_MAX_RETRY_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(2 ** attempt)

        return []

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------
    async def search(
        self,
        client_code: str,
        auth_headers: dict[str, str],
        account_id: str,
        query: str,
    ) -> list[TargetingEntity]:
        """Perform a general targeting search across interests, behaviors, and demographics."""
        return await self._fetch_with_retry(
            client_code=client_code,
            auth_headers=auth_headers,
            account_id=account_id,
            endpoint="targetingsearch",
            params={"q": query},
        )

    # ------------------------------------------------------------------
    # fetch_interests
    # ------------------------------------------------------------------
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
        # Phase 1: parallel search per seed
        search_tasks = [
            self._fetch_with_retry(
                client_code=client_code,
                auth_headers=auth_headers,
                account_id=account_id,
                endpoint="targetingsearch",
                params={"q": seed, "limit_type": "interests"},
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
            )
            for batch in id_batches
        ]
        suggestion_results = await asyncio.gather(*suggestion_tasks, return_exceptions=True)

        all_entities = list(unique_map.values())
        for result in suggestion_results:
            if isinstance(result, list):
                all_entities.extend(result)

        return _deduplicate(all_entities)

    # ------------------------------------------------------------------
    # fetch_behaviors
    # ------------------------------------------------------------------
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
        browse_task = self._fetch_with_retry(
            client_code=client_code,
            auth_headers=auth_headers,
            account_id=account_id,
            endpoint="targetingbrowse",
            params={"limit_type": "behaviors"},
        )
        search_tasks = [
            self._fetch_with_retry(
                client_code=client_code,
                auth_headers=auth_headers,
                account_id=account_id,
                endpoint="targetingsearch",
                params={"q": seed, "limit_type": "behaviors"},
            )
            for seed in seeds
        ]

        all_results = await asyncio.gather(*search_tasks, browse_task, return_exceptions=True)

        entities: list[TargetingEntity] = []
        for result in all_results:
            if isinstance(result, list):
                entities.extend(result)

        return _deduplicate(entities)

    # ------------------------------------------------------------------
    # fetch_demographics  (fixed catalog — no seeds needed)
    # ------------------------------------------------------------------
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
        browse_tasks = [
            self._fetch_with_retry(
                client_code=client_code,
                auth_headers=auth_headers,
                account_id=account_id,
                endpoint="targetingbrowse",
                params={"limit_type": subtype},
            )
            for subtype in DEMOGRAPHIC_FIXED_SUBTYPES
        ]

        all_results = await asyncio.gather(*browse_tasks, return_exceptions=True)

        entities: list[TargetingEntity] = []
        for result in all_results:
            if isinstance(result, list):
                entities.extend(result)

        return _deduplicate(entities)

    # ------------------------------------------------------------------
    # search_open_demographics  (job titles / employers / majors)
    # ------------------------------------------------------------------
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
        search_tasks = [
            self._fetch_with_retry(
                client_code=client_code,
                auth_headers=auth_headers,
                account_id=account_id,
                endpoint="targetingsearch",
                params={"q": seed, "limit_type": subtype},
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

    # ------------------------------------------------------------------
    # validate
    # ------------------------------------------------------------------
    async def validate(
        self,
        client_code: str,
        auth_headers: dict[str, str],
        account_id: str,
        entities: list[TargetingEntity],
    ) -> list[TargetingEntity]:
        """Verify targeting entities are still active in Meta's system.

        Batches by 50, uses GET targetingvalidation with targeting_list query param.
        Returns only entities where valid=True. Falls back to returning the batch
        unvalidated on error so the pipeline never produces empty results.
        """
        if not entities:
            return []

        clean_id = _normalize_account_id(account_id)
        full_endpoint = f"/act_{clean_id}/targetingvalidation"
        batch_size = 50
        batches = [entities[i: i + batch_size] for i in range(0, len(entities), batch_size)]

        async def _validate_batch(batch: list[TargetingEntity]) -> list[TargetingEntity]:
            try:
                targeting_list = [e.to_validation_pair() for e in batch if e.id]
                response = await meta_client.get(
                    endpoint=full_endpoint,
                    client_code=client_code,
                    auth_headers=auth_headers,
                    params={"targeting_list": json.dumps(targeting_list)},
                )
                if "error" in response:
                    logger.warning("targetingvalidation error - returning batch unvalidated")
                    return batch

                active_ids = {
                    str(item.get("id"))
                    for item in response.get("data", [])
                    if item.get("valid") is True
                }
                return [e for e in batch if str(e.id) in active_ids]
            except Exception as exc:
                logger.warning("Validation batch failed: %s - returning unvalidated", exc)
                return batch

        results = await asyncio.gather(*[_validate_batch(b) for b in batches])
        return [entity for batch_result in results for entity in batch_result]

