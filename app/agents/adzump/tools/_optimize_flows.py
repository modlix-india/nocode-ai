"""Storage-read and fresh-analysis flows for the optimize tool, plus the typed
``OptimizationRequest`` they share. ``optimize.py`` stays a thin dispatcher that
builds the request and routes to one of these flows.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import TypeAdapter

from app.config import settings
from app.agents.adzump.agents.optimization.agent import get_optimization_agent
from app.agents.adzump.recommendations.models import CampaignRecommendation
from app.agents.adzump.agents.optimization.platform_registry import (
    get_platform_conversion_signal_fn,
)
from app.agents.adzump.agents.optimization.resolver import resolve_platform_and_account
from app.agents.adzump.agents.optimization.craft import (
    emit_campaign_recommendations_craft,
)
from app.agents.adzump.services.recommendation_storage import (
    recommendation_storage_service,
)
from app.core.agent import spawn_sub_agent
from app.core.session import BaseSession, AuthContext
from app.core.streaming import AgentEventStream
from app.core.tools.base import ToolResult

logger = logging.getLogger(__name__)

OPTIMIZATION_MODEL_OVERRIDE = settings.ADZUMP_OPTIMIZATION_MODEL


def parse_bool(params: dict, key: str) -> bool:
    """Read a boolean tool param, tolerating LLM-stringified values
    (``"true"``/``"1"``/``"yes"``). Missing/empty → False."""
    val = params.get(key, False)
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes")
    return bool(val)


@dataclass(frozen=True)
class OptimizationRequest:
    """Request-scoped invariants for one optimize tool call — built once at the
    entry point, then threaded through the flows as a single object instead of the
    ``(campaign_id, client_code, headers, session, auth, event_stream, craft_id,
    …)`` clump."""

    campaign_id: str
    platform: str | None
    question: str
    fresh: bool
    force_refresh: bool
    client_code: str
    headers: dict
    session: BaseSession | None
    auth: AuthContext | None
    event_stream: AgentEventStream | None
    tool_use_id: str
    craft_id: str

    @classmethod
    def from_tool_call(cls, params: dict, context: dict) -> "OptimizationRequest":
        """Build the request from raw LLM params + tool context, normalizing the
        platform and resolving the craft id at this single boundary."""
        from app.agents.adzump.agents.optimization.platform_registry import (
            normalize_platform,
        )

        platform = str(params.get("platform") or "").strip() or None
        if platform:
            platform = normalize_platform(platform)

        session: BaseSession | None = context.get("_session")
        session_ctx = context.get("session_context", {}) or {}
        craft_id = session_ctx.get("craft_id")
        if not craft_id and session:
            craft_id = (
                session.context.get("craft_id") or f"adzump_{session.session_id[:8]}"
            )
        if not craft_id:
            craft_id = "adzump_opt"

        return cls(
            campaign_id=str(params.get("campaign_id") or "").strip(),
            platform=platform,
            question=str(params.get("question") or "").strip(),
            fresh=parse_bool(params, "fresh"),
            force_refresh=parse_bool(params, "force_refresh"),
            client_code=context.get("client_code", ""),
            headers=context.get("headers", {}),
            session=session,
            auth=context.get("auth"),
            event_stream=context.get("event_stream"),
            tool_use_id=context.get("tool_use_id", ""),
            craft_id=craft_id,
        )


async def _safe_emit_craft(
    event_stream: AgentEventStream | None,
    craft_id: str | None,
    recommendations: Any,
    title: str,
    log_context: str,
) -> None:
    """Emit a Craft panel update without crashing the agent if rendering fails."""
    if not event_stream or not craft_id:
        return
    try:
        await emit_campaign_recommendations_craft(
            event_stream=event_stream,
            craft_id=craft_id,
            recommendations=recommendations,
            title=title,
        )
    except Exception:
        logger.warning(
            f"optimize: failed to emit craft from {log_context}", exc_info=True
        )


def _summarize_stored_recommendation(stored: Any) -> list[str]:
    """Format a stored CampaignRecommendation into summary lines."""
    lines = [
        f"{stored.campaign_name} ({stored.campaign_id})",
        f"Platform: {stored.platform}",
        f"Generated: {stored.generated_at} (source: {stored.source})",
    ]
    try:
        from app.agents.adzump.agents.optimization.platform_registry import get_provider

        provider = get_provider(stored.platform)
        if provider and stored.fields:
            lines.extend(provider.summarize_fields(stored.fields))
    except Exception:
        logger.info("summarize_stored_recommendation failed", exc_info=True)
    return lines


# Flow — serve stored recommendations from storage (no sub-agent, no LLM cost)
async def _serve_from_storage(
    req: OptimizationRequest, prefetched_latest: dict | None = None
) -> ToolResult:
    """Read stored recommendations and return immediately. ``prefetched_latest``
    lets the cooldown gate hand in the single-campaign record it already fetched,
    avoiding a duplicate ``get_latest`` round trip."""
    logger.info(
        "optimize: serve_from_storage START campaign=%s platform=%s client=%s",
        req.campaign_id or "(all)",
        req.platform or "(any)",
        req.client_code,
    )

    if not req.campaign_id:
        stored_dicts = await recommendation_storage_service.get_active_recommendations(
            campaign_id=None,
            client_code=req.client_code,
            auth_headers=req.headers,
        )
        stored_items = []
        raw_items = []
        for item in stored_dicts:
            try:
                rec = TypeAdapter(CampaignRecommendation).validate_python(item)
                if req.platform and rec.platform.upper() != req.platform.upper():
                    continue
                stored_items.append(rec)
            except Exception:
                logger.warning(
                    "optimize: stored recommendation parse failed record_id=%s",
                    item.get("_id", "unknown"),
                    exc_info=True,
                )
                if not req.platform or item.get("platform", "").upper() == req.platform.upper():
                    raw_items.append(item)

        logger.info(
            "optimize: serve_from_storage found %d valid, %d raw (unparsed) records",
            len(stored_items),
            len(raw_items),
        )

        if stored_items or raw_items:
            await _safe_emit_craft(
                req.event_stream,
                req.craft_id,
                stored_items,
                "Stored Account Optimization",
                "storage list",
            )

            lines = [
                f"Stored recommendations for client {req.client_code}:",
                f"Total active recommendation bundle(s): {len(stored_items) + len(raw_items)}",
                "",
            ]
            for idx, stored in enumerate(stored_items[:20], start=1):
                summary = _summarize_stored_recommendation(stored)
                lines.append(f"{idx}. " + summary[0])
                for detail in summary[1:]:
                    lines.append(f"   {detail}")
                lines.append("")
            if len(stored_items) > 20:
                lines.append(f"... and {len(stored_items) - 20} more")
            if raw_items:
                lines.append(
                    f"{len(raw_items)} stored record(s) could not be parsed into the current schema."
                )

            return ToolResult(
                success=True,
                data={
                    "stored": [s.model_dump() for s in stored_items],
                    "raw_unparsed": raw_items,
                    "source": "storage",
                    "count": len(stored_items) + len(raw_items),
                },
                summary="\n".join(lines).strip(),
            )

        await _safe_emit_craft(
            req.event_stream, req.craft_id, None, "No Recommendations", "empty storage list"
        )

        logger.info(
            "optimize: serve_from_storage NO records found client=%s",
            req.client_code,
        )
        return ToolResult(
            success=True,
            data={"stored": [], "source": "storage", "count": 0},
            summary=(
                f"No stored active recommendations found for client {req.client_code}."
            ),
        )

    stored_dict = (
        prefetched_latest
        if prefetched_latest is not None
        else await recommendation_storage_service.get_latest(
            campaign_id=req.campaign_id,
            client_code=req.client_code,
            auth_headers=req.headers,
        )
    )
    stored = None
    if stored_dict:
        try:
            stored = TypeAdapter(CampaignRecommendation).validate_python(stored_dict)
            logger.info(
                "optimize: serve_from_storage FOUND stored rec "
                "campaign=%s name=%s platform=%s",
                stored.campaign_id,
                stored.campaign_name,
                stored.platform,
            )
        except Exception:
            logger.warning(
                "optimize: serve_from_storage stored rec parse failed campaign=%s",
                req.campaign_id,
                exc_info=True,
            )
    else:
        logger.info(
            "optimize: serve_from_storage NO stored rec for campaign=%s",
            req.campaign_id,
        )
    if stored:
        await _safe_emit_craft(
            req.event_stream,
            req.craft_id,
            stored,
            f"Recommendations: {stored.campaign_name}",
            "storage single",
        )

        lines = [
            f"Stored recommendations for campaign '{stored.campaign_name}' (ID: {stored.campaign_id}):",
            *_summarize_stored_recommendation(stored)[1:],
        ]

        return ToolResult(
            success=True,
            data={
                "stored": stored.model_dump(),
                "source": "storage",
                "campaign_id": stored.campaign_id,
                "campaign_name": stored.campaign_name,
                "product_name": stored.product_name,
            },
            summary="\n".join(lines),
        )

    # Not in DB — resolve campaign name/product to a real id for the fresh-analysis prompt.
    resolved_campaign_id = req.campaign_id
    resolved_campaign_name = None
    try:
        res = await resolve_platform_and_account(
            req.campaign_id, req.client_code, req.headers, {}, live_scan=False
        )
        resolved_campaign_id = res.get("campaign_id", req.campaign_id)
        if res.get("mapping"):
            resolved_campaign_name = res["mapping"].get("name")
    except Exception:
        logger.debug(
            "optimize: serve_from_storage fallback resolution failed", exc_info=True
        )

    await _safe_emit_craft(
        req.event_stream, req.craft_id, None, "No Recommendations", "fallback storage"
    )

    campaign_desc = (
        f"'{resolved_campaign_name}' (ID: {resolved_campaign_id})"
        if resolved_campaign_name
        else f"'{req.campaign_id}'"
    )
    return ToolResult(
        success=True,
        data={
            "stored": None,
            "source": "storage",
            "campaign_id": resolved_campaign_id,
            "prompt_fresh_analysis": True,
        },
        summary=(
            f"No stored recommendations found for campaign {campaign_desc}. "
            "Tell the user that no pre-calculated nightly recommendations are available for this campaign, "
            "and explicitly ask them if they would like to trigger a fresh live analysis now. "
            "Do not output raw 'Yes/No' text options in your chat response, as the UI will render interactive choice buttons for them."
        ),
    )


# Resolve — lookup platform, account, and product id for a campaign
async def _resolve_campaign_context(req: OptimizationRequest) -> dict[str, Any] | None:
    """Resolve platform, account, and product from mapping/storage/handlers.

    All resolved fields come strictly from the campaign's own data
    (mapping, stored recommendations, or live API scan).  The parent
    session context is NEVER consulted for platform, account, or product
    fields — this prevents cross-product pollution when a user manages
    multiple products in the same session.

    Returns a dict with keys: campaign_id, account_id, login_customer_id,
        platform, product_id, product_name, campaign_name.
    Returns None if account metadata cannot be resolved.
    """
    campaign_id = req.campaign_id
    resolved = {
        "campaign_id": campaign_id,
        "account_id": "",
        "login_customer_id": "",
        "platform": "",
        "product_id": "",
        "product_name": "",
        "campaign_name": "",
        "mapping_exists": False,
        "resolved_mapping": None,
    }

    try:
        res = await resolve_platform_and_account(
            campaign_id, req.client_code, req.headers, req.session.context
        )
        resolved["campaign_id"] = res.get("campaign_id", campaign_id)

        if res.get("platform"):
            resolved["platform"] = res["platform"]
        if res.get("account_id"):
            resolved["account_id"] = res["account_id"]
        if res.get("login_customer_id"):
            resolved["login_customer_id"] = res["login_customer_id"]
        if res.get("product_id"):
            resolved["product_id"] = res["product_id"]
        if res.get("product_name"):
            resolved["product_name"] = res["product_name"]

        mapping = res.get("mapping")
        if mapping:
            resolved["mapping_exists"] = True
            resolved["resolved_mapping"] = mapping
            if mapping.get("name"):
                resolved["campaign_name"] = mapping["name"]
    except Exception:
        logger.warning(
            "optimize: campaign mapping lookup failed campaign=%s",
            campaign_id,
            exc_info=True,
        )

    if not resolved["account_id"] or not resolved["login_customer_id"]:
        logger.warning("optimize: account metadata unresolved campaign=%s", campaign_id)
        return None

    logger.info(
        "optimize: campaign_context resolved campaign=%s platform=%s "
        "account=%s product=%s",
        campaign_id,
        resolved["platform"],
        resolved["account_id"],
        resolved["product_id"],
    )
    return resolved


# Flow — spawn OptimizationAgent sub-agent for fresh analysis
async def _fresh_analysis(req: OptimizationRequest) -> ToolResult:
    """Run OptimizationAgent as a sub-agent and store results.

    Precondition: session/auth/event_stream are non-None — the dispatcher guards
    them before routing here, so this flow uses ``req.session.context`` directly.
    """
    campaign_id = req.campaign_id
    logger.info(
        "optimize: fresh_analysis START campaign=%s client=%s%s",
        campaign_id,
        req.client_code,
        f" question='{req.question[:80]}" if req.question else "",
    )

    ctx = await _resolve_campaign_context(req)
    if ctx is None:
        logger.warning(
            "optimize: fresh_analysis ABORTED — account metadata unresolved "
            "campaign=%s client=%s",
            campaign_id,
            req.client_code,
        )
        return ToolResult(
            success=False,
            error=(
                f"I couldn't find the ad account details for campaign {campaign_id}. "
                "This campaign may not be linked to a Google or Meta ad account yet. "
                "Please make sure the campaign is properly set up and linked before "
                "running an analysis."
            ),
        )

    campaign_id = ctx.get("campaign_id", campaign_id)

    if not ctx.get("platform"):
        logger.warning(
            "optimize: fresh_analysis ABORTED — platform unresolved campaign=%s",
            campaign_id,
        )
        return ToolResult(
            success=False,
            error=(
                f"Campaign {campaign_id} could not be associated with a supported ad platform. "
                "Please ensure the campaign has a valid Google Ads or Meta ad account linked."
            ),
        )

    sub_context: dict[str, object] = {
        "account_id": ctx["account_id"],
        "login_customer_id": ctx["login_customer_id"],
        "active_campaign_id": campaign_id,
        "campaign_spec": req.session.context.get("campaign_spec", {}),
        "product_id": ctx["product_id"],
        "product_name": ctx.get("product_name", ""),
        "platform": ctx["platform"],
        "campaign_name": ctx.get("campaign_name", campaign_id),
        "mapping_exists": ctx.get("mapping_exists", False),
        "resolved_mapping": ctx.get("resolved_mapping"),
        "_campaign_contexts": req.session.context.get("_campaign_contexts", []),
    }

    compute_signal = get_platform_conversion_signal_fn(ctx["platform"])
    if compute_signal:
        try:
            signal = await compute_signal(
                campaign_id=campaign_id,
                customer_id=ctx["account_id"],
                login_customer_id=ctx["login_customer_id"],
                client_code=req.client_code,
                auth_headers=req.headers,
            )
            if signal:
                sub_context["conversion_signal"] = signal.model_dump()
                logger.info(
                    "optimize: signal_prefetch campaign=%s status=%s",
                    campaign_id,
                    signal.status,
                )
        except Exception:
            logger.warning(
                "optimize: conversion signal pre-fetch failed campaign=%s",
                campaign_id,
                exc_info=True,
            )

    from app.agents.adzump.agents.optimization.platform_handlers import (
        get_platform_handler,
    )

    handler = get_platform_handler(ctx["platform"])
    if handler:
        try:
            overview = await handler.fetch_campaign_overview(
                campaign_id=campaign_id,
                account_id=ctx["account_id"],
                login_customer_id=ctx["login_customer_id"],
                client_code=req.client_code,
                auth_headers=req.headers,
            )
            if overview:
                sub_context["_overview"] = overview.model_dump()
                ct = getattr(overview, "campaign_type", None)
                if ct:
                    sub_context["campaign_type"] = ct
                if getattr(overview, "campaign_name", None):
                    sub_context["campaign_name"] = overview.campaign_name
        except Exception:
            logger.warning(
                "optimize: failed to fetch campaign overview campaign=%s",
                campaign_id,
                exc_info=True,
            )

    logger.info(
        "optimize: fresh_analysis sub-agent launching campaign=%s "
        "platform=%s account=%s product=%s",
        campaign_id,
        ctx["platform"],
        ctx["account_id"],
        ctx.get("product_id", "(none)"),
    )

    user_message = req.question or (
        f"Run a fresh analysis for campaign {campaign_id}. "
        "Check conversion health first, then get budget/bidding and keyword recommendations."
    )

    try:
        sub_session = await spawn_sub_agent(
            agent=get_optimization_agent(),
            user_message=user_message,
            initial_context=sub_context,
            parent_session=req.session,
            parent_stream=req.event_stream,
            parent_tool_use_id=req.tool_use_id,
            auth=req.auth,
            timeout=300,
            model_override=OPTIMIZATION_MODEL_OVERRIDE,
        )
    except asyncio.TimeoutError:
        logger.error(
            "optimize: fresh_analysis TIMEOUT campaign=%s (300s limit)",
            campaign_id,
        )
        return ToolResult(
            success=False,
            error=(
                f"The analysis for campaign {campaign_id} took too long and was stopped. "
                "This can happen with large campaigns. Please try again, or contact support "
                "if the problem persists."
            ),
        )
    except Exception as e:
        logger.error(
            "optimize: fresh_analysis FAILED campaign=%s error=%s",
            campaign_id,
            str(e),
            exc_info=True,
        )
        return ToolResult(
            success=False,
            error=(
                f"The analysis for campaign {campaign_id} encountered an error. "
                "Our team has been notified. Please try again later."
            ),
        )

    return await _extract_and_store_results(sub_session, campaign_id, req)


def _build_campaign_recommendation(
    context: dict, campaign_id: str, platform: str, provider: Any
) -> tuple[Any, bool]:
    """Pure: assemble the stored recommendation bundle from a finished sub-agent's
    session context. Returns ``(rec, has_actions)``. No I/O — unit-testable without
    storage or craft. Skips come from the SAME provider gate the LLM's tool list
    used, so the stored record and the live conversation can't disagree about them."""
    fields = provider.build_fields_from_session_context(context)
    has_actions = provider.has_actionable_recommendations(fields)

    campaign_type = context.get("campaign_type", "UNKNOWN")
    has_mapping = bool(context.get("mapping_exists"))
    skipped_analyses = [
        {"section": skip.section, "campaign_type": campaign_type, "reason": skip.reason}
        for _name, skip in provider.applicable_tools_for_campaign(campaign_type, has_mapping)
        if skip is not None and skip.section is not None
    ]

    rec_data = {
        "platform": platform,
        "parent_account_id": context.get("login_customer_id", ""),
        "account_id": context.get("account_id", ""),
        "product_id": context.get("product_id", ""),
        "product_name": context.get("product_name", ""),
        "campaign_id": campaign_id,
        "campaign_name": context.get("campaign_name", campaign_id),
        "campaign_type": campaign_type,
        "skipped_analyses": skipped_analyses,
        "completed": False,
        "active": True,
        "source": "user_requested",
        "fields": fields,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    rec = TypeAdapter(CampaignRecommendation).validate_python(rec_data)
    return rec, has_actions


# Extract — read sub-agent results from session context and persist to storage
async def _extract_and_store_results(
    sub_session: BaseSession,
    campaign_id: str,
    req: OptimizationRequest,
) -> ToolResult:
    """Extract fresh recommendations from sub-session context and persist them."""
    fresh_recs = sub_session.context.get("_fresh_recommendations", {})
    campaign_fresh = fresh_recs.get(campaign_id, {})

    if not campaign_fresh:
        errors = sub_session.context.get("_errors", [])
        if errors:
            err_details = "; ".join([f"{e['tool']}: {e['error']}" for e in errors])
            logger.error(
                "optimize: extract_results NO recommendations — tool errors: %s",
                err_details,
            )
            return ToolResult(
                success=False,
                error=(
                    f"The analysis for campaign {campaign_id} could not generate "
                    "recommendations due to data access issues. Please check that "
                    "your ad account is properly connected and try again."
                ),
            )
        logger.warning(
            "optimize: extract_results complete but no recommendations generated campaign=%s",
            campaign_id,
        )
        return ToolResult(
            success=False,
            error=(
                f"Analysis completed for campaign {campaign_id} but produced no recommendations. "
                "The campaign may lack sufficient data, or all diagnostic tools returned empty results. "
                "Verify the campaign has recent activity and try again."
            ),
        )

    # Validate platform and provider BEFORE the storage try block so config errors
    # are reported with accurate messages rather than the generic "couldn't save" text.
    from app.agents.adzump.agents.optimization.platform_registry import get_provider

    platform = sub_session.context.get("platform")
    if not platform:
        return ToolResult(
            success=False,
            error="Could not determine the ad platform for this campaign. Please ensure the campaign platform is resolved before running an analysis.",
        )

    provider = get_provider(platform)
    if not provider:
        logger.error(
            "optimize: extract_results no provider for platform=%s campaign=%s",
            platform,
            campaign_id,
        )
        return ToolResult(
            success=False,
            error=f"Platform '{platform}' is not configured in the optimization system. This is a configuration issue — please contact support.",
        )

    if not provider.capabilities.has_recommendations:
        return ToolResult(
            success=False,
            error=f"Live recommendations are not yet supported for {platform} campaigns.",
        )

    try:
        rec, has_actions = _build_campaign_recommendation(
            sub_session.context, campaign_id, platform, provider
        )

        auth_headers = sub_session.auth.to_headers() if sub_session.auth else None
        if auth_headers is None:
            logger.warning(
                "optimize: extract_results sub_session.auth is None — "
                "storage call will be unauthenticated campaign=%s",
                campaign_id,
            )
        try:
            await recommendation_storage_service.store(
                rec, req.client_code, auth_headers=auth_headers
            )
            logger.info(
                "optimize: extract_results STORED campaign=%s platform=%s has_actions=%s",
                campaign_id,
                rec.platform,
                has_actions,
            )
        except Exception as store_err:
            # Storage failure is non-fatal: the analysis succeeded and the Craft panel
            # can still be shown. Results won't be available on the next "show me recs"
            # request, but the current turn is still useful.
            logger.warning(
                "optimize: extract_results STORE FAILED (non-fatal) campaign=%s error=%s",
                campaign_id,
                str(store_err),
            )

        craft_title = (
            f"Fresh Recommendations: {rec.campaign_name}"
            if has_actions
            else f"Analysis: {rec.campaign_name}"
        )
        await _safe_emit_craft(
            req.event_stream, req.craft_id, rec, craft_title, "fresh analysis"
        )

        # Tools ran but found no actionable issues — campaign is healthy for the queried area.
        if not has_actions:
            return ToolResult(
                success=True,
                data={"source": "fresh_analysis", "campaign_id": campaign_id, "healthy": True},
                summary=(
                    f"Analysis complete for campaign {campaign_id}. "
                    "No actionable issues were found — the campaign is performing within expected ranges. "
                    "Tell the user their campaign looks healthy and no changes are required at this time."
                ),
            )

    except Exception as e:
        logger.error(
            "optimize: extract_results FAILED campaign=%s error=%s",
            campaign_id,
            str(e),
            exc_info=True,
        )
        return ToolResult(
            success=False,
            error=(
                f"The analysis failed for campaign {campaign_id}. Please try again."
            ),
        )

    summary_lines = [f"Fresh analysis complete for campaign {campaign_id}."]

    mapping_exists = sub_session.context.get("mapping_exists", True)
    if not mapping_exists:
        from app.agents.adzump.agents.optimization.platform_registry import (
            build_optimization_tool_map,
        )

        tools = build_optimization_tool_map(sub_session.context.get("platform"))
        product_dependent = [
            t.name for t in tools.values() if t.requires_product_mapping
        ]
        if product_dependent:
            skipped_str = ", ".join(product_dependent)
            summary_lines.append(
                f"Note: The following product-dependent tools were skipped because this campaign lacks a product mapping: [{skipped_str}]. Explicitly mention this to the user."
            )

    return ToolResult(
        success=True,
        data={"source": "fresh_analysis", "campaign_id": campaign_id},
        summary="\n".join(summary_lines),
    )
