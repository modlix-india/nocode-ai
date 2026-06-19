"""Shared helpers for channel-type gating in the Google optimization tools:
resolving a campaign's advertising channel type, returning an honest
"not applicable for this campaign type" result, and a single entry guard that
prepares a tool's inputs or stops it when the analysis doesn't apply.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional, Union

from app.core.tools.base import ToolResult
from app.agents.adzump.adapters.google.gaql import is_numeric_id
from app.agents.adzump.recommendations.models import ChannelType
from app.agents.adzump.recommendations.google.capabilities import (
    get_capabilities,
    ChannelCapabilities,
)
from app.agents.adzump.adapters.google.client import google_ads_client

logger = logging.getLogger(__name__)


async def get_campaign_channel_type(campaign_id: str, context: dict) -> str:
    """Return a campaign's ``advertising_channel_type`` (e.g. "PERFORMANCE_MAX").

    For the active campaign the type is already known (set by the scheduler
    kwarg or the chat overview as ``campaign_type``). Only when the caller passes
    a DIFFERENT campaign id (chat: the LLM may name another campaign) do we fall
    back to a lazy per-id cache + one GAQL query. Anything we can't resolve →
    ``"UNKNOWN"`` so the caller uses conservative capabilities.
    """
    session_ctx = context.get("session_context") or {}
    cid = str(campaign_id or "").strip()
    if not cid:
        return ChannelType.UNKNOWN.value
    if cid == str(session_ctx.get("active_campaign_id", "")) and session_ctx.get("campaign_type"):
        return session_ctx["campaign_type"]

    cache = session_ctx.setdefault("_campaign_types", {})
    if cid in cache:
        return cache[cid]

    account_id = str(session_ctx.get("account_id", "") or "")
    login_customer_id = str(session_ctx.get("login_customer_id", "") or "")
    # campaign.id is numeric; guard before interpolating into GAQL.
    if not account_id or not is_numeric_id(cid):
        return ChannelType.UNKNOWN.value

    try:
        rows = await google_ads_client.search(
            query=(
                "SELECT campaign.advertising_channel_type "
                f"FROM campaign WHERE campaign.id = {cid}"
            ),
            customer_id=account_id,
            login_customer_id=login_customer_id,
            client_code=context.get("client_code", ""),
            auth_headers=context.get("headers", {}),
        )
        channel = ""
        if rows:
            channel = (rows[0].get("campaign", {}) or {}).get(
                "advertisingChannelType", ""
            )
        resolved = channel or ChannelType.UNKNOWN.value
    except Exception:
        logger.warning(
            "get_campaign_channel_type: GAQL failed campaign=%s — defaulting UNKNOWN",
            cid,
            exc_info=True,
        )
        resolved = ChannelType.UNKNOWN.value

    cache[cid] = resolved
    return resolved


def not_applicable_result(
    *, section: str, campaign_id: str, campaign_type: str, reason: str
) -> ToolResult:
    """Honest "not applicable" outcome — success=True (an expected skip, not an
    error, so the agent doesn't apologise about a data problem). Carries the
    structured skip so the caller can record a SkippedAnalysis."""
    return ToolResult(
        success=True,
        data={
            "not_applicable": True,
            "section": section,
            "campaign_id": campaign_id,
            "campaign_type": campaign_type,
            "reason": reason,
        },
        summary=(
            f"{section.replace('_', ' ').title()} analysis is not applicable to "
            f"this campaign ({campaign_type}). {reason}"
        ),
    )


@dataclass
class PreparedTool:
    """Resolved inputs a Google campaign tool needs to run."""

    account_id: str
    login_customer_id: str
    campaign_type: str
    mapping: Optional[dict]


async def prepare_google_campaign_tool(
    context: dict,
    campaign_id: str,
    *,
    section: str,
    requires_capability: Optional[Callable[[ChannelCapabilities], bool]] = None,
    requires_mapping: bool = False,
) -> Union[PreparedTool, ToolResult]:
    """Resolve a Google tool's inputs (account, channel type, product mapping) or
    return an early ``ToolResult`` to hand straight back. One call replaces the
    repeated read-account → guard → channel-check → mapping boilerplate at the top
    of each tool, and applies the channel-capability gate (defense in depth for
    the chat path, where the LLM may invoke a tool on any campaign id). Errors
    carry a structured ``error_code`` so the agent layer owns the phrasing.
    """
    session_ctx = context.get("session_context") or {}
    cid = str(campaign_id or "").strip()
    if not cid:
        return ToolResult(
            success=False,
            error="campaign_id is required.",
            data={"error_code": "missing_campaign_id"},
        )

    # Google campaign ids are numeric; reject anything else before it reaches a
    # GAQL query (defense in depth for the chat path, where the LLM may pass a name).
    if not is_numeric_id(cid):
        return ToolResult(
            success=False,
            error=f"'{campaign_id}' is not a valid campaign id.",
            data={"error_code": "invalid_campaign_id", "campaign_id": cid},
        )

    # Account is resolved upstream (resolve_platform_and_account) into session_ctx.
    account_id = str(session_ctx.get("account_id", "") or "")
    login_customer_id = str(session_ctx.get("login_customer_id", "") or "")
    if not account_id:
        return ToolResult(
            success=False,
            error="The ad account for this campaign couldn't be determined.",
            data={"error_code": "account_unresolved", "campaign_id": cid},
        )

    # Channel-capability gate.
    campaign_type = await get_campaign_channel_type(cid, context)
    if requires_capability is not None:
        caps = get_capabilities(campaign_type)
        if not requires_capability(caps):
            return not_applicable_result(
                section=section,
                campaign_id=cid,
                campaign_type=campaign_type,
                reason=f"{caps.display_label} campaigns don't support it.",
            )

    # Product mapping — resolved upstream; fetch a one-shot fallback only when the
    # tool needs it (channel-agnostic tools skip the fetch entirely).
    mapping = session_ctx.get("resolved_mapping")
    if requires_mapping and not mapping:
        from app.agents.adzump.agents.optimization.mapping_service import (
            fetch_campaign_mappings,
        )

        all_mappings = await fetch_campaign_mappings(
            context.get("client_code", ""), context.get("headers", {})
        )
        mapping = all_mappings.get(cid)
        if not mapping:
            return ToolResult(
                success=False,
                error=(
                    f"Campaign {cid} is not linked to any product yet — this analysis "
                    "needs product details to generate recommendations."
                ),
                data={"error_code": "mapping_missing", "campaign_id": cid},
            )

    return PreparedTool(
        account_id=account_id,
        login_customer_id=login_customer_id,
        campaign_type=campaign_type,
        mapping=mapping,
    )
