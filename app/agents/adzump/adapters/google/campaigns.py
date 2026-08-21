"""The atomic googleAds:mutate endpoint — creating a campaign and everything it needs.

Transport only. What the operations SAY is the campaign builder's business (see the campaign
agent's emitter); this owns the endpoint, the request envelope and the response shape.

https://developers.google.com/google-ads/api/docs/mutating/overview
"""

from __future__ import annotations

from typing import Any

from app.agents.adzump.adapters.google.client import google_ads_client


async def mutate(
    *,
    customer_id: str,
    operations: list[dict[str, Any]],
    validate_only: bool = False,
    login_customer_id: str = "",
    client_code: str = "",
    auth_headers: dict[str, str] | None = None,
) -> dict:
    """Post one all-or-nothing batch. Raises GoogleAdsApiError on a non-2xx.

    validate_only checks without committing, and returns no resource names.
    """
    payload: dict[str, Any] = {
        "mutateOperations": operations,
        # Sent explicitly though false is the default: it is what makes the batch atomic,
        # and a campaign must never be half created.
        "partialFailure": False,
    }
    if validate_only:
        payload["validateOnly"] = True

    return await google_ads_client.post(
        f"customers/{customer_id}/googleAds:mutate",
        payload,
        client_code,
        auth_headers or {},
        login_customer_id or None,
    )


def created_campaign(response: dict) -> str:
    """The campaign's resource name, or "" if the response holds none."""
    # Results come back positionally, one per operation, so this reads the campaign's own
    # result rather than assuming an index.
    for result in response.get("mutateOperationResponses") or []:
        if name := (result.get("campaignResult") or {}).get("resourceName"):
            return name
    return ""
