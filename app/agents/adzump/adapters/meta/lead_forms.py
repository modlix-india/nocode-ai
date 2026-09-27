"""Meta Lead Forms adapter - fetches and manages Lead Forms via Graph API."""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any

import httpx

from app.agents.adzump.adapters.meta.client import meta_client

logger = logging.getLogger(__name__)

# Maximum cursor hops per paginated Graph API call.
_MAX_PAGE_ITERATIONS: int = 10

# Maximum historical lead forms fetched per page.
_MAX_LEADGEN_FORMS: int = 500

# Retry configuration for transient Graph API failures
_MAX_ADAPTER_RETRIES: int = 3
_STATUS_CODE_RE = re.compile(r"Meta Graph API (\d{3})")
_ERROR_CODE_RE = re.compile(r"code (\d+)")

# Known permanent Meta error codes that must never be retried
_NON_RETRYABLE_META_CODES = frozenset({
    10,   # Permission Denied
    100,  # Invalid Parameter / Schema violation
    190,  # Invalid / Expired OAuth Access Token
    200,  # Permissions error
    368,  # Temporarily Blocked (Policy/Abuse block, 48h+)
    506,  # Duplicate Post
})


class MetaLeadFormsAdapter:
    """Adapter for Meta Instant Forms operations (Graph API)."""

    def _raise_missing_page_token(self, page_id: str) -> None:
        """Raise a descriptive error when a Page Access Token cannot be resolved."""
        raise RuntimeError(
            f"Could not resolve a Page Access Token for Facebook Page {page_id}. "
            "Ensure the connected Meta account has an active role on this Page and "
            "the required permissions are granted (pages_manage_ads, pages_read_engagement, leads_retrieval)."
        )

    async def _call_with_retry(
        self,
        func,
        *args,
        is_mutation: bool = False,
        max_retries: int = _MAX_ADAPTER_RETRIES,
        **kwargs,
    ) -> Any:
        """Executes a Meta API call with exponential backoff and jitter for transient errors.

        - Idempotent reads (get_leadgen_forms, get_page_info) safely retry up to max_retries.
        - Permanent client errors (400, 401, 403, 404, or known permanent error codes) fail fast immediately.
        - Non-idempotent writes (create_leadgen_form) do not auto-retry on unknown timeouts (is_mutation=True)
          to protect against creating duplicate permanent live forms on Meta.
        """
        attempts = 1 if is_mutation else max_retries
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                last_error = e
                err_str = str(e)

                # Check for explicit HTTP status code in standard error message
                match_status = _STATUS_CODE_RE.search(err_str)
                if match_status:
                    status_code = int(match_status.group(1))
                    # Fail fast on all 4xx client errors except 429 (Rate Limit / Too Many Requests)
                    if 400 <= status_code < 500 and status_code != 429:
                        raise

                # Check for explicit permanent Meta error codes
                match_code = _ERROR_CODE_RE.search(err_str)
                if match_code:
                    code_val = int(match_code.group(1))
                    if code_val in _NON_RETRYABLE_META_CODES:
                        raise

                # For mutations, do not retry on generic timeouts or errors
                if is_mutation:
                    raise

                if attempt < attempts - 1:
                    delay = (2 ** attempt) + random.uniform(0.1, 0.4)
                    logger.warning(
                        "MetaLeadFormsAdapter transient failure (attempt %d/%d). Retrying in %.2fs: %s",
                        attempt + 1, attempts, delay, e,
                    )
                    await asyncio.sleep(delay)
                    continue

                raise last_error

        if last_error:
            raise last_error

    async def get_leadgen_forms(
        self,
        page_id: str,
        client_code: str,
        auth_headers: dict[str, str],
        limit: int = 100,
        active_only: bool = True,
        page_token: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch lead generation forms for a Facebook Page with cursor pagination.

        Follows cursor-based pagination up to `_MAX_LEADGEN_FORMS`. Forms are
        sorted by `leads_count` descending so top-performing forms appear first.
        Filters for ACTIVE forms, falling back to all collected forms if none are active.
        """
        token = page_token or await self._get_page_token(page_id, client_code, auth_headers)
        if not token:
            self._raise_missing_page_token(page_id)

        fields = (
            "id,name,status,created_time,leads_count,"
            "questions,context_card,thank_you_page,"
            "privacy_policy_url,is_optimized_for_quality"
        )

        collected: list[dict[str, Any]] = []
        after_cursor: str | None = None

        for _ in range(_MAX_PAGE_ITERATIONS):
            params: dict[str, Any] = {"fields": fields, "limit": limit}
            if after_cursor:
                params["after"] = after_cursor

            result = await self._call_with_retry(
                meta_client.get,
                f"/{page_id}/leadgen_forms",
                client_code=client_code,
                auth_headers=auth_headers,
                params=params,
                access_token=token,
            )
            batch = result.get("data", [])
            collected.extend(batch)

            if len(collected) >= _MAX_LEADGEN_FORMS:
                collected = collected[:_MAX_LEADGEN_FORMS]
                logger.debug(
                    "get_leadgen_forms: reached _MAX_LEADGEN_FORMS=%d for page %s",
                    _MAX_LEADGEN_FORMS, page_id,
                )
                break

            paging = result.get("paging", {})
            if not paging.get("next"):
                break

            after_cursor = (paging.get("cursors") or {}).get("after")
            if not after_cursor:
                break

        collected.sort(key=lambda f: int(f.get("leads_count") or 0), reverse=True)

        if active_only and collected:
            active_forms = [
                f for f in collected if (f.get("status") or "").upper() == "ACTIVE"
            ]
            return active_forms or collected
        return collected


    async def create_leadgen_form(
        self,
        page_id: str,
        form_payload: dict[str, Any],
        client_code: str,
        auth_headers: dict[str, str],
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """Creates a new lead generation form for a Facebook Page."""
        token = page_token or await self._get_page_token(page_id, client_code, auth_headers)
        if not token:
            self._raise_missing_page_token(page_id)

        result = await self._call_with_retry(
            meta_client.post,
            endpoint=f"/{page_id}/leadgen_forms",
            client_code=client_code,
            auth_headers=auth_headers,
            json=form_payload,
            access_token=token,
            is_mutation=True,
        )
        return result

    async def upload_cover_photo(
        self,
        page_id: str,
        file_bytes: bytes,
        filename: str,
        content_type: str,
        client_code: str,
        auth_headers: dict[str, str],
        page_token: str | None = None,
    ) -> dict[str, str]:
        """Uploads user image bytes directly to the Facebook Page as an unpublished photo.
        Returns:
            {"photo_id": "1023456789", "source_url": "https://scontent...fbcdn.net/..."}
        """
        token = page_token or await self._get_page_token(page_id, client_code, auth_headers)
        if not token:
            self._raise_missing_page_token(page_id)

        files = {
            "source": (filename, file_bytes, content_type)
        }
        data = {
            "published": "false",  # Keep private/unpublished, don't post to public timeline
        }

        result = await self._call_with_retry(
            meta_client.post,
            endpoint=f"/{page_id}/photos",
            client_code=client_code,
            auth_headers=auth_headers,
            data=data,
            files=files,
            params={"fields": "id,source,images"},
            access_token=token,
            max_retries=2,
        )

        return {
            "photo_id": str(result.get("id", "")),
            "source_url": result.get("source", ""),
        }

    async def get_page_info(
        self,
        page_id: str,
        client_code: str,
        auth_headers: dict[str, str],
    ) -> dict[str, str | None]:
        """Resolve Page Access Token and profile picture URL for a Facebook Page.

        Queries /me/accounts with cursor pagination (limit=300), stopping as soon
        as the target page is found. Falls back to querying /{page_id} directly
        if not found in /me/accounts.
        """
        access_token: str | None = None
        picture_url: str | None = None
        accounts_fetched = False
        pages_inspected: int = 0
        after_cursor: str | None = None

        try:
            for _ in range(_MAX_PAGE_ITERATIONS):
                params: dict[str, Any] = {
                    "fields": "id,access_token,picture.type(large){url}",
                    "limit": 300,
                }
                if after_cursor:
                    params["after"] = after_cursor

                pages_data = await self._call_with_retry(
                    meta_client.get,
                    "/me/accounts",
                    client_code=client_code,
                    auth_headers=auth_headers,
                    params=params,
                )
                accounts_fetched = True
                batch = pages_data.get("data", [])
                pages_inspected += len(batch)

                for page in batch:
                    if str(page.get("id")) == str(page_id):
                        access_token = page.get("access_token")
                        picture_url = (
                            ((page.get("picture") or {}).get("data") or {}).get("url")
                        )
                        break

                if access_token:
                    break

                paging = pages_data.get("paging", {})
                if not paging.get("next"):
                    break

                after_cursor = (paging.get("cursors") or {}).get("after")
                if not after_cursor:
                    break

        except Exception as e:
            logger.warning("Failed to fetch /me/accounts for page %s: %s", page_id, e)

        # Fallback to /{page_id} directly if not found in /me/accounts.
        if not access_token:
            if accounts_fetched:
                logger.info(
                    "Page %s not found in /me/accounts after inspecting %d account(s). "
                    "Attempting direct fallback fetch.",
                    page_id, pages_inspected,
                )
            try:
                page_info = await self._call_with_retry(
                    meta_client.get,
                    f"/{page_id}",
                    client_code=client_code,
                    auth_headers=auth_headers,
                    params={"fields": "access_token,picture.type(large){url}"},
                )
                access_token = page_info.get("access_token")
                if not picture_url:
                    picture_url = (
                        ((page_info.get("picture") or {}).get("data") or {}).get("url")
                    )
            except Exception as e:
                logger.warning("Failed fallback page info fetch for %s: %s", page_id, e)

        return {
            "access_token": access_token,
            "picture_url": picture_url,
        }

    async def _get_page_token(
        self, page_id: str, client_code: str, auth_headers: dict[str, str]
    ) -> str | None:
        """Resolve the Page Access Token for a given Page ID."""
        info = await self.get_page_info(page_id, client_code, auth_headers)
        return info.get("access_token")

meta_lead_forms_adapter = MetaLeadFormsAdapter()
