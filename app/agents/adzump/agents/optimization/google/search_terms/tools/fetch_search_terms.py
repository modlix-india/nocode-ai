from __future__ import annotations

import asyncio
from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump.agents.optimization.google.search_terms.adapters import (
    GoogleSearchTermAdapter,
)

from structlog import get_logger

logger = get_logger(__name__)

adapter = GoogleSearchTermAdapter()


async def _fetch_search_terms(params: dict, context: dict) -> ToolResult:
    session_ctx = context.get("session_context") or {}
    client_code = context.get("client_code") or session_ctx.get("client_code", "")
    auth_headers = context.get("headers") or session_ctx.get("headers") or {}
    
    accounts_to_fetch = params.get("accounts")
    if not accounts_to_fetch:
        # Compatibility with old single-account mode
        accounts_to_fetch = [{
            "customer_id": params.get("account_id"),
            "login_customer_id": params.get("parent_account_id")
        }]

    all_results = []
    
    # Run all fetches in parallel to stay fast
    tasks = []
    for acc in accounts_to_fetch:
        cid = acc.get("customer_id")
        lcid = acc.get("login_customer_id")
        if cid and lcid:
            tasks.append(adapter.fetch_search_terms(
                account_id=cid,
                parent_account_id=lcid,
                client_code=client_code,
                auth_headers=auth_headers
            ))
    
    if not tasks:
        return ToolResult(success=True, data={"search_terms": [], "count": 0})
        
    results_list = await asyncio.gather(*tasks)
    for r in results_list:
        all_results.extend(r)

    return ToolResult(
        success=True,
        data={"search_terms": all_results, "count": len(all_results)},
    )


fetch_search_terms = ToolDefinition(
    name="fetch_search_terms",
    description="Fetch Google Ads search terms for multiple accounts.",
    parameters=[
        ToolParameter(
            name="accounts",
            type="array",
            description="List of accounts with customer_id and login_customer_id",
            required=False,
        ),
        ToolParameter(
            name="account_id",
            type="string",
            description="Legacy single Google Ads account ID",
            required=False,
        ),
        ToolParameter(
            name="parent_account_id",
            type="string",
            description="Legacy single Parent/MCC account ID",
            required=False,
        ),
    ],
    execute=_fetch_search_terms,
)
