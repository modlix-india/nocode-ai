"""Google Ads Keyword Planner Adapter.

Fetches keyword ideas and historical metrics from the Google Ads Keyword Planner
API (generateKeywordIdeas / generateHistoricalMetrics endpoints).
"""

import asyncio
import logging
from typing import List, Dict, Any, Optional

from app.agents.adzump.adapters.google.client import (
    google_ads_client, 
    _raise_for_google_error, 
    _extract_retry_delay
)
from app.agents.adzump.utils.http_client import http_request

logger = logging.getLogger(__name__)


class GoogleKeywordPlannerAdapter:
    CHUNK_SIZE = 15
    CHUNK_DELAY = 0.5
    DEFAULT_LOCATION_IDS = ["geoTargetConstants/2356"]
    DEFAULT_LANGUAGE_ID = 1000
    MIN_KEYWORD_LENGTH = 2
    MAX_KEYWORD_WORDS = 6

    def __init__(self):
        self.client = google_ads_client

    async def generate_keyword_ideas(
        self,
        customer_id: str,
        login_customer_id: str,
        client_code: str,
        seed_keywords: List[str],
        url: Optional[str] = None,
        location_ids: Optional[List[str]] = None,
        language_id: int = DEFAULT_LANGUAGE_ID,
        auth_headers: dict = None,
    ) -> List[Dict[str, Any]]:
        """Fetch keyword ideas from Google Ads Keyword Planner API.

        Seeds are chunked and processed sequentially. Results are deduplicated
        by keyword text, keeping the entry with the highest search volume.
        """
        location_ids = location_ids or self.DEFAULT_LOCATION_IDS
        token = await self.client._get_api_token(client_code, auth_headers)
        headers = self.client._build_auth_headers(token, login_customer_id)
        endpoint = (
            f"{self.client.BASE_URL}/{self.client.API_VERSION}"
            f"/customers/{customer_id}:generateKeywordIdeas"
        )

        seen: Dict[str, Dict[str, Any]] = {}

        for i in range(0, len(seed_keywords), self.CHUNK_SIZE):
            chunk = seed_keywords[i : i + self.CHUNK_SIZE]
            chunk_num = i // self.CHUNK_SIZE + 1

            payload = _build_payload(chunk, url, location_ids, language_id)
            logger.info(f"keyword_planner_chunk chunk={chunk_num} seeds={len(chunk)}")

            try:
                # POST to the Google Ads REST endpoint for keyword ideas
                response = await http_request(
                    "POST",
                    endpoint,
                    headers=headers,
                    json=payload,
                    error_handler=_raise_for_google_error,
                    retry_delay_parser=_extract_retry_delay,
                )
                ideas = response.json().get("results", [])
            except Exception:
                logger.exception(
                    "keyword_planner_chunk_failed chunk=%d seeds=%s",
                    chunk_num, chunk[:5],
                )
                continue

            for idea in ideas:
                parsed = _parse_keyword_idea(idea)
                if parsed:
                    _deduplicate(seen, parsed)

            if i + self.CHUNK_SIZE < len(seed_keywords):
                await asyncio.sleep(self.CHUNK_DELAY)

        results = sorted(seen.values(), key=lambda k: k["volume"], reverse=True)
        logger.info(f"keyword_planner_done total={len(results)}")
        return results

    async def generate_historical_metrics(
        self,
        customer_id: str,
        login_customer_id: str,
        client_code: str,
        keywords: List[str],
        location_ids: Optional[List[str]] = None,
        language_id: int = DEFAULT_LANGUAGE_ID,
        auth_headers: dict = None,
    ) -> List[Dict[str, Any]]:
        """Fetch historical search metrics for specific keyword texts from Google Ads API."""
        if not keywords:
            return []
        location_ids = location_ids or self.DEFAULT_LOCATION_IDS
        token = await self.client._get_api_token(client_code, auth_headers)
        headers = self.client._build_auth_headers(token, login_customer_id)
        endpoint = (
            f"{self.client.BASE_URL}/{self.client.API_VERSION}"
            f"/customers/{customer_id}:generateHistoricalMetrics"
        )

        results = []

        for i in range(0, len(keywords), self.CHUNK_SIZE):
            chunk = keywords[i : i + self.CHUNK_SIZE]
            chunk_num = i // self.CHUNK_SIZE + 1

            payload = {
                "keywords": chunk,
                "language": f"languageConstants/{language_id}",
                "geoTargetConstants": list(location_ids),
                "keywordPlanNetwork": "GOOGLE_SEARCH_AND_PARTNERS",
            }
            logger.info(f"generate_historical_metrics_chunk chunk={chunk_num} keywords={len(chunk)}")

            response = await http_request(
                "POST",
                endpoint,
                headers=headers,
                json=payload,
                error_handler=_raise_for_google_error,
                retry_delay_parser=_extract_retry_delay,
            )

            for result in response.json().get("results", []):
                parsed = _parse_historical_result(result)
                if parsed:
                    results.append(parsed)

            if i + self.CHUNK_SIZE < len(keywords):
                await asyncio.sleep(self.CHUNK_DELAY)

        logger.info(f"generate_historical_metrics_done total={len(results)}")
        return results



def _build_payload(
    chunk: List[str],
    url: Optional[str],
    location_ids: List[str],
    language_id: int,
) -> Dict[str, Any]:
    payload = {
        "language": f"languageConstants/{language_id}",
        "geoTargetConstants": list(location_ids),
        "includeAdultKeywords": False,
        "keywordPlanNetwork": "GOOGLE_SEARCH_AND_PARTNERS",
    }
    if url and url.strip():
        payload["keywordAndUrlSeed"] = {"keywords": chunk, "url": url.strip()}
    else:
        payload["keywordSeed"] = {"keywords": chunk}
    return payload


def _parse_keyword_idea(idea: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    text = (idea.get("text") or "").strip().lower()
    if len(text) < GoogleKeywordPlannerAdapter.MIN_KEYWORD_LENGTH:
        return None
    if len(text.split()) > GoogleKeywordPlannerAdapter.MAX_KEYWORD_WORDS:
        return None

    metrics = idea.get("keywordIdeaMetrics", {})
    return {
        "keyword": text,
        "volume": int(metrics.get("avgMonthlySearches", 0) or 0),
        "competition": metrics.get("competition", "UNKNOWN"),
        "competitionIndex": float(metrics.get("competitionIndex", 0) or 0) / 100.0,
    }


def _deduplicate(seen: Dict[str, Dict[str, Any]], entry: Dict[str, Any]) -> None:
    keyword = entry["keyword"]
    existing = seen.get(keyword)
    if existing is None or entry["volume"] > existing["volume"]:
        seen[keyword] = entry


def _parse_historical_result(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    text = (result.get("text") or "").strip().lower()
    if not text:
        return None
    if len(text) < GoogleKeywordPlannerAdapter.MIN_KEYWORD_LENGTH:
        return None
    if len(text.split()) > GoogleKeywordPlannerAdapter.MAX_KEYWORD_WORDS:
        return None

    metrics = result.get("keywordMetrics", {})
    return {
        "keyword": text,
        "volume": int(metrics.get("avgMonthlySearches", 0) or 0),
        "competition": metrics.get("competition", "UNKNOWN"),
        "competitionIndex": float(metrics.get("competitionIndex", 0) or 0) / 100.0,
    }



# Singleton instance
keyword_planner_adapter = GoogleKeywordPlannerAdapter()
