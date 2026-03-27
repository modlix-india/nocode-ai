"""Account management tools — Google Ads and Meta account selection.

These tools help users select their advertising accounts before campaign creation.
"""

from __future__ import annotations

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.tools._shared import get_ds_client, build_ds_headers


async def _fetch_google_accounts(params: dict, context: dict) -> ToolResult:
    """Fetch Google Ads manager accounts (MCCs) accessible by the user."""
    client = get_ds_client()
    headers = build_ds_headers(context)

    result = await client.get(
        "/api/ds/ads/accounts/google",
        headers=headers,
    )

    if not result.success:
        return result

    return ToolResult(
        success=True,
        data=result.data,
        summary="Retrieved Google Ads manager accounts.",
    )


async def _fetch_google_child_accounts(params: dict, context: dict) -> ToolResult:
    """Fetch Google Ads accounts under a specific MCC."""
    mcc_id = params.get("mcc_id", "").strip()
    if not mcc_id:
        return ToolResult(success=False, error="mcc_id is required.")

    client = get_ds_client()
    headers = build_ds_headers(context)

    result = await client.get(
        f"/api/ds/ads/accounts/google/{mcc_id}",
        headers=headers,
    )

    if not result.success:
        return result

    return ToolResult(
        success=True,
        data=result.data,
        summary=f"Retrieved ad accounts under MCC {mcc_id}.",
    )


async def _fetch_meta_accounts(params: dict, context: dict) -> ToolResult:
    """Fetch Meta/Facebook ad accounts accessible by the user."""
    client = get_ds_client()
    headers = build_ds_headers(context)

    result = await client.get(
        "/api/ds/ads/accounts/meta",
        headers=headers,
    )

    if not result.success:
        return result

    return ToolResult(
        success=True,
        data=result.data,
        summary="Retrieved Meta ad accounts.",
    )


fetch_google_accounts = ToolDefinition(
    name="fetch_google_accounts",
    description="List Google Ads manager accounts (MCCs) accessible by the user. First step in Google Ads account selection.",
    display_name="Fetch Google Accounts",
    parameters=[],
    execute=_fetch_google_accounts,
)

fetch_google_child_accounts = ToolDefinition(
    name="fetch_google_child_accounts",
    description="List Google Ads accounts under a specific manager account (MCC). Call after fetch_google_accounts to let user choose an ad account.",
    display_name="Fetch Google Child Accounts",
    parameters=[
        ToolParameter(name="mcc_id", type="string", description="The Google Ads MCC (manager) account ID", required=True),
    ],
    execute=_fetch_google_child_accounts,
)

fetch_meta_accounts = ToolDefinition(
    name="fetch_meta_accounts",
    description="List Meta/Facebook ad accounts accessible by the user.",
    display_name="Fetch Meta Accounts",
    parameters=[],
    execute=_fetch_meta_accounts,
)

ACCOUNT_TOOLS = [fetch_google_accounts, fetch_google_child_accounts, fetch_meta_accounts]
