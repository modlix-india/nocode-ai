"""Product analysis tool — runs the ProductAnalyst sub-agent to scrape + profile
a business website. The sub-agent owns the live Playwright scrape + gpt-4o profile."""

from __future__ import annotations

import logging

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump._shared import emit_progress

logger = logging.getLogger(__name__)

_ANALYST_USER_MESSAGE = (
    "Analyze this business website: {url}\n\n"
    "SCOPE: Scrape the homepage ONCE and generate a product profile. "
    "Do NOT scrape sub-pages — one scrape_url call only. "
    "Do NOT call web_search or web_fetch. "
    "After scraping, write the final JSON with the 'business' section filled "
    "and an empty 'competitive' section."
)

async def _analyze_product(params: dict, context: dict) -> ToolResult:
    """Spawn the Product Analyst agent to scrape + generate a product profile.
    Thin bridge: cache check → spawn agent → persist → return."""
    import time as _time
    _run_start = _time.monotonic()

    url = (params.get("url") or "").strip()
    if not url:
        return ToolResult(success=False, error="url is required.")
    # Force https:// so storage keys / event payloads / API calls stay consistent
    # (mixed http+https double-keyed the same business).
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    elif not url.startswith("https://"):
        url = f"https://{url}"

    stream = context.get("event_stream")
    tool_use_id = context.get("tool_use_id", "")
    auth = context.get("auth")
    session_ctx = context.get("session_context", {}) or {}
    parent_session = context.get("_session")
    target_ctx = parent_session.context if parent_session else session_ctx

    # Already analyzed this session → return cached.
    existing_business = (
        (parent_session.context.get("product_data") if parent_session else None)
        or session_ctx.get("product_data")
    )
    
    if existing_business:
        name = existing_business.get("product_name", "product")
        return ToolResult(
            success=True,
            data={"business": existing_business},
            summary=f"Already analyzed: {name}. No need to re-analyze.",
        )

    # Cross-session cache: URL analyzed in a prior session → skip the scrape.
    cached_result = await _serve_from_storage(url, stream, context, target_ctx)
    if cached_result is not None:
        return cached_result

    try:
        from app.agents.adzump.agents.product.agent import get_product_agent
        from app.core.streaming import pre_emit_agent_started

        await emit_progress(context, "Starting product analysis…")
        # Launcher owns both AgentCard ends: agent_started here, agent_finished after.
        await pre_emit_agent_started(
            stream, agent_id="product_analyst", label="Product Analyst",
            parent_tool_use_id=tool_use_id, context=context,
        )
        output = await get_product_agent().analyze(
            url=url,
            parent_event_stream=stream,
            parent_tool_use_id=tool_use_id,
            auth=auth,
            parent_session_context=session_ctx,
            user_message=_ANALYST_USER_MESSAGE.format(url=url),
        )

        if not output.business:
            raise RuntimeError("Agent produced no usable result")

        business = output.business
        if output.screenshot_url:
            business["screenshot_url"] = output.screenshot_url

        # Persist to parent session. Ownership:
        #   product_data: MERGE not replace — the sub-agent put runtime artifacts here
        #     (site_links, screenshot, scrape_count); wholesale replace wipes them.
        #     JSON keys (product_name, summary, …) win on conflict.
        #   product_profile: sub-agent owns .summary — only refresh url + title here.
        #   competitor_analysis: owned here, set when competitive output exists.
        target_ctx["product_data"] = {**(target_ctx.get("product_data") or {}), **business}
        profile = target_ctx.setdefault("product_profile", {})
        profile["url"] = url
        profile["title"] = business.get("product_name", "") or profile.get("title", "")
        # Fallback: seed summary only if the sub-agent didn't run (legacy bypass).
        if not profile.get("summary"):
            profile["summary"] = business.get("summary", "")
        if output.competitive and output.competitive.get("competitors"):
            target_ctx["competitor_analysis"] = output.competitive

        duration_ms = int((_time.monotonic() - _run_start) * 1000)
        await _safe_emit_finished(
            stream, status="success", duration_ms=duration_ms,
            summary=f"Analyzed {business.get('product_name', 'product')}",
        )

        # Emit the asset-upload prompt on the PARENT stream (the sub-agent's
        # _PassthroughEventStream drops emit_text, so the picker can't ask here).
        elicited = await _emit_asset_upload_prompt(stream, target_ctx, url)

        result_data: dict = {"business": business}
        if elicited:
            # Deferred elicitation: asked for uploads → break the run loop, yield
            # the turn. expects="multi": uploads may span several messages.
            result_data["elicited"] = True
            result_data["elicit_expects"] = "multi"

        return ToolResult(
            success=True,
            data=result_data,
            summary=_build_llm_summary(business),
        )

    except Exception as e:
        logger.warning("analyze_product failed: %s: %s",
                       type(e).__name__, str(e)[:200])
        await _safe_emit_finished(stream, status="error", summary=str(e)[:100])
        return ToolResult(
            success=False,
            error=f"Product analysis failed: {type(e).__name__}: {e}",
        )


async def _serve_from_storage(url: str, stream, context: dict, target_ctx: dict) -> ToolResult | None:
    """Cross-session cache: if this URL was analyzed before, hydrate target_ctx +
    re-render the craft panel (same UI as a fresh scrape) and return the reuse
    ToolResult. Returns None on miss/error — caller falls through to a fresh scrape."""
    try:
        from app.agents.adzump.services.business_storage import hydrate_from_storage
        from app.agents.adzump.tools.competitor import _emit_final_craft
        if not await hydrate_from_storage(url, target_ctx, context):
            return None
        business = target_ctx.get("product_data") or {}
        name = business.get("product_name", "product")
        await emit_progress(context, f"Reused stored analysis for {name}")

        craft_id = target_ctx.get("craft_id", "")
        if stream and craft_id:
            competitive = target_ctx.get("competitor_analysis") or {"competitors": []}
            try:
                await _emit_final_craft(
                    stream, craft_id, url, business, competitive,
                    screenshot_url=(business.get("primary_screenshot_url")
                                    or business.get("screenshot_url")),
                    baked_summary=business.get("summary", ""),
                )
            except Exception as e:
                logger.warning("storage_hydrate_craft_failed: %s: %s",
                               type(e).__name__, str(e)[:200])
        return ToolResult(
            success=True,
            data={"business": business, "from_storage": True},
            summary=(
                f"Reused prior analysis for {name} from storage. "
                f"Type: {business.get('business_type', '')}. "
                f"Location: {business.get('location', '')}. "
                "Tell the user we're picking up where things left off."
            ),
        )
    except Exception as e:
        logger.warning("storage_hydrate_skipped: %s: %s", type(e).__name__, str(e)[:200])
        return None


async def _safe_emit_finished(stream, **kwargs) -> None:
    """emit_agent_finished for product_analyst, swallowing stream errors."""
    if stream is None:
        return
    try:
        await stream.emit_agent_finished(agent_id="product_analyst", **kwargs)
    except Exception:
        pass


def _build_llm_summary(business: dict) -> str:
    """The tool-result summary the orchestrator LLM sees. Deliberately OMITs location
    — echoing it made the LLM ask "confirm location?" as free text before
    confirm_location's widget fired (dup question); it stays in product_data."""
    lines: list[str] = []
    if business.get("product_name"):
        lines.append(f"Product: {business['product_name']}")
    if business.get("business_type"):
        lines.append(f"Type: {business['business_type']}")
    if business.get("summary"):
        lines.append(f"\n{business['summary']}")
    return "\n".join(lines) or "Product analysis complete."


# ── Asset-upload prompt (orchestrator layer) ──────────────────────────
# Emitted here, not in the picker: the sub-agent's _PassthroughEventStream drops
# emit_text. Signal stashed on product_data["_shift3_signal"] by the picker.

async def _emit_asset_upload_prompt(stream, target_ctx: dict, url: str) -> bool:
    """Emit the asset-upload prompt if the picker left gaps; no-op otherwise.
    Returns True iff a prompt was emitted — caller flags the result as a
    (deferred, multi) elicitation so the run loop yields the turn."""
    if stream is None:
        return False
    signal = (target_ctx.get("product_data") or {}).get("_shift3_signal") or {}
    missing_logo = bool(signal.get("logo_missing"))
    missing_creatives = list(signal.get("creative_missing_categories") or [])
    if not missing_logo and not missing_creatives:
        return False  # picker output complete — nothing to ask

    from app.agents.adzump.agents.product.tools.scrape.assets import (
        _compose_asset_request_text,  # pure, unit-tested composer
    )
    text = _compose_asset_request_text(missing_logo, missing_creatives)
    try:
        await stream.emit_text(f"\n\n{text}\n")
        await stream.emit_data("asset_upload_request", {
            "stage": "1",
            "url": url,
            "logo_missing": missing_logo,
            "creative_missing_categories": missing_creatives,
            "blocking": False,
        })
    except Exception:
        logger.exception("asset_request_prompt_emit_failed url=%s", url)
        return False

    # Quick-action chip so the user needn't type to continue (upload optional).
    # On target_ctx so get_pending_suggestions returns it this turn — popped
    # before the elicitation guard, so the open upload elicitation can't suppress it.
    target_ctx["_pending_suggestions"] = {
        "options": [{"label": "Continue without uploading",
                     "value": "Continue without uploading"}],
        "mode": "single",
    }

    logger.info(
        "asset_request_prompt_shown: url=%s stage=1 missing_logo=%s missing_creatives=%s",
        url, missing_logo, missing_creatives,
    )
    return True


# ── Tool definition ───────────────────────────────────────────────────

analyze_business = ToolDefinition(
    name="analyze_product",
    description=(
        "Scrape a business website and generate a product profile: what they sell "
        "(all variants), location, pricing, USPs, target customer. This is the FIRST "
        "step when the user gives a URL. Does NOT do competitor research — that "
        "happens separately later. Returns a structured product profile."
    ),
    display_name="Analyze Product",
    parameters=[
        ToolParameter(name="url", type="string", description="The business website URL to analyze.", required=True),
    ],
    execute=_analyze_product,
)

BUSINESS_TOOLS = [analyze_business]
