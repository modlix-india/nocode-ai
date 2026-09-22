"""Meta Lead Forms adapter - fetches and manages Lead Forms via Graph API."""

from __future__ import annotations

import logging
from typing import Any

from app.agents.adzump.adapters.meta.client import meta_client

logger = logging.getLogger(__name__)


class MetaLeadFormsAdapter:
    """Adapter for Meta Instant Forms operations (Graph API)."""
    
    async def get_leadgen_forms(
        self,
        page_id: str,
        client_code: str,
        auth_headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Fetch all lead generation forms for a Facebook Page."""
        page_token = await self._get_page_token(page_id, client_code, auth_headers)
        if not page_token:
            raise RuntimeError(
                f"Could not find a Page Access Token for page {page_id}. "
                "Ensure the connected Meta user has manage_pages permission."
            )

        fields = (
            "id,name,status,created_time,leads_count,expired_leads_count,questions,"
            "context_card,thank_you_page,privacy_policy_url,tracking_parameters,locale,"
            "is_optimized_for_quality,page"
        )
        
        result = await meta_client.get(
            f"/{page_id}/leadgen_forms",
            client_code=client_code,
            auth_headers=auth_headers,
            params={"fields": fields},
            access_token=page_token,
        )
        return result.get("data", [])

    async def create_leadgen_form(
        self,
        page_id: str,
        form_payload: dict[str, Any],
        client_code: str,
        auth_headers: dict[str, str],
    ) -> dict[str, Any]:
        """Creates a new lead generation form for a Facebook Page."""
        page_token = await self._get_page_token(page_id, client_code, auth_headers)
        if not page_token:
            raise RuntimeError(f"Could not find Page Access Token for {page_id}")
            
        result = await meta_client.post(
            endpoint=f"/{page_id}/leadgen_forms",
            client_code=client_code,
            auth_headers=auth_headers,
            json=form_payload,
            access_token=page_token
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
    ) -> dict[str, str]:
        """Uploads user image bytes directly to the Facebook Page as an unpublished photo.
        Returns:
            {"photo_id": "1023456789", "source_url": "https://scontent...fbcdn.net/..."}
        """
        page_token = await self._get_page_token(page_id, client_code, auth_headers)
        if not page_token:
            raise RuntimeError(f"Could not find Page Access Token for {page_id}")

        files = {
            "source": (filename, file_bytes, content_type)
        }
        data = {
            "published": "false",  # Keep private/unpublished, don't post to public timeline
        }

        result = await meta_client.post(
            endpoint=f"/{page_id}/photos",
            client_code=client_code,
            auth_headers=auth_headers,
            data=data,
            files=files,
            params={"fields": "id,source,images"},
            access_token=page_token,
        )

        return {
            "photo_id": str(result.get("id", "")),
            "source_url": result.get("source", ""),
        }

    async def get_page_profile_picture(
        self,
        page_id: str,
        client_code: str,
        auth_headers: dict[str, str],
    ) -> str | None:
        """Fetch Page profile picture URL from Meta Graph API."""
        try:
            page_token = await self._get_page_token(page_id, client_code, auth_headers)
            res = await meta_client.get(
                f"/{page_id}",
                client_code=client_code,
                auth_headers=auth_headers,
                params={"fields": "picture.type(large){url},name"},
                access_token=page_token,
            )
            return res.get("picture", {}).get("data", {}).get("url")
        except Exception as e:
            logger.warning(f"Could not fetch page picture for {page_id}: {e}")
            return None

    async def _get_page_token(
        self, page_id: str, client_code: str, auth_headers: dict[str, str]
    ) -> str | None:
        """Resolve the Page Access Token for a given Page ID.
        Reuses the resolution pattern from accounts.py.
        """
        pages_data = await meta_client.get(
            "/me/accounts",
            client_code=client_code,
            auth_headers=auth_headers,
            params={"fields": "id,access_token", "limit": 100},
        )
        
        page_token = next(
            (p.get("access_token") for p in pages_data.get("data", [])
             if str(p.get("id")) == str(page_id)),
            None,
        )
        
        # Fallback: ask for the token directly on the page if not found in /me/accounts
        if not page_token:
            try:
                page_info = await meta_client.get(
                    f"/{page_id}",
                    client_code=client_code,
                    auth_headers=auth_headers,
                    params={"fields": "access_token"},
                )
                page_token = page_info.get("access_token")
            except Exception:
                pass
                
        return page_token

meta_lead_forms_adapter = MetaLeadFormsAdapter()
