"""CustomAudience — read the account's segments, create one.

Cannot be created inside the campaign's atomic build: MutateOperation has no
custom_audience_operation. It must exist first, then be referenced by resource name.
validateOnly here checks parsing and required fields only - it accepts an over-length
keyword and even zero members - so the published limits are ours to enforce. Verified live.
"""

from __future__ import annotations

import logging

from app.agents.adzump.adapters.google.client import google_ads_client

logger = logging.getLogger(__name__)

# "Use AUTO instead of these 2 options when creating a new custom audience" (INTEREST /
# PURCHASE_INTENT). validateOnly accepts INTEREST anyway, so this is ours to get right.
CREATE_TYPE = "AUTO"

# GAQL returns removed resources unless filtered, and reusing a REMOVED one attaches a dead
# reference. ENABLED / REMOVED are the only two values.
_LIST_QUERY = (
    "SELECT custom_audience.resource_name, custom_audience.name, "
    "custom_audience.description FROM custom_audience "
    "WHERE custom_audience.status = 'ENABLED'"
)


async def list_enabled(
    *,
    customer_id: str,
    login_customer_id: str = "",
    client_code: str = "",
    auth_headers: dict[str, str] | None = None,
) -> list[dict]:
    """Every live custom audience on the account, as ``{resource_name, name, description}``."""
    rows = await google_ads_client.search_stream(
        _LIST_QUERY,
        customer_id,
        login_customer_id,
        client_code,
        auth_headers or {},
    )
    out: list[dict] = []
    for row in rows:
        ca = row.get("customAudience") or {}
        if ca.get("resourceName"):
            out.append(
                {
                    "resource_name": ca["resourceName"],
                    "name": ca.get("name") or "",
                    "description": ca.get("description") or "",
                }
            )
    return out


async def create(
    *,
    customer_id: str,
    name: str,
    description: str,
    keywords: list[str],
    urls: list[str] | None = None,
    apps: list[str] | None = None,
    login_customer_id: str = "",
    client_code: str = "",
    auth_headers: dict[str, str] | None = None,
) -> str:
    """Create one custom audience from keywords, URLs and/or apps; returns its resource name.

    status and id are OUTPUT_ONLY and never sent - the API accepts them silently.
    PLACE_CATEGORY is the one member type left out: it is in the proto but the service
    rejects it at request parsing.
    """
    members = [{"memberType": "KEYWORD", "keyword": k} for k in keywords]
    members += [{"memberType": "URL", "url": u} for u in urls or []]
    members += [{"memberType": "APP", "app": a} for a in apps or []]
    if not members:
        raise ValueError("a custom audience needs at least one member")

    payload = await google_ads_client.post(
        f"customers/{customer_id}/customAudiences:mutate",
        {
            "operations": [
                {
                    "create": {
                        "name": name,
                        "type": CREATE_TYPE,
                        "description": description,
                        "members": members,
                    }
                }
            ]
        },
        client_code,
        auth_headers or {},
        login_customer_id or None,
    )
    results = payload.get("results") or []
    resource_name = (results[0] or {}).get("resourceName") if results else ""
    if not resource_name:
        raise ValueError(f"customAudiences:mutate returned no resource name: {payload}")
    logger.info(
        "custom audience created: %s (%d keywords, %d urls)",
        resource_name,
        len(keywords),
        len(urls or []),
    )
    return resource_name
