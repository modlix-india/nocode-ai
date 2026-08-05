"""Product analysis tool - runs the ProductAnalyst sub-agent to scrape + profile
a business website. The sub-agent owns the live Playwright scrape + gpt-4o profile."""

from __future__ import annotations

import logging

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump._shared import emit_progress, primary_screenshot_url
from app.agents.adzump.models.product import check_product

logger = logging.getLogger(__name__)


async def _analyze_product(params: dict, context: dict) -> ToolResult:
    """Spawn the Product Analyst agent to scrape + generate a product profile.
    Thin bridge: cache check → spawn agent → persist → return."""

    url = _normalize_url(params.get("url", ""))
    if not url:
        return ToolResult(success=False, error="url is required.")

    stream = context.get("event_stream")
    tool_use_id = context.get("tool_use_id", "")
    auth = context.get("auth")
    # memory arrives as live obj (_session) or snapshot dict (session_context);
    # use _session.context (the one that persists), else snapshot.
    session_obj = context.get("_session")
    session_memory = session_obj.context if session_obj else (context.get("session_context") or {})

    # already analyzed this session → return stored product, no re-scrape.
    existing_product = session_memory.get("product_data")

    if existing_product:
        name = existing_product.get("product_name", "product")
        return ToolResult(
            success=True,
            data={"product": existing_product},
            summary=f"Already analyzed: {name}. No need to re-analyze.",
        )

    # Cross-session cache: URL analyzed in a prior session → skip the scrape.
    cached_result = await _serve_from_storage(url, stream, context, session_memory)
    if cached_result is not None:
        return cached_result

    try:
        analysis = await _run_product_agent(url, stream, tool_use_id, auth, session_memory)

        # analysis.product = scraped profile dict; None/{} if scrape found nothing → abort below.
        product = analysis.product
        if not product:
            raise RuntimeError("Agent produced no usable result")

        _merge_product_into_context(session_memory, analysis, url)

        # Emit the full craft panel (badge + key-values) immediately after analysis,
        # before geo-targeting runs. Map section is omitted because no target_areas yet.
        craft_id = session_memory.get("craft_id") or session_memory.get("_craft_id", "")
        if stream and craft_id:
            from app.agents.adzump.tools.craft import emit_craft_panel as _emit_final_craft
            product_now = session_memory.get("product_data") or {}
            early_summary = (
                (session_memory.get("product_profile") or {}).get("summary")
                or product_now.get("summary", "")
            )
            try:
                await _emit_final_craft(
                    stream, craft_id, url, product_now,
                    session_memory.get("competitor_analysis") or {},
                    screenshot_url=primary_screenshot_url(product_now),
                    baked_summary=early_summary,
                )
            except Exception as e:
                logger.warning("post_analyze_craft_emit_failed: %s", e)

        await _safe_emit_finished(
            stream, status="success",
            summary=f"Analyzed {product.get('product_name', 'product')}",
        )

        # upload prompt fires on PARENT stream (picker can't: _PassthroughEventStream
        # drops emit_text). reads typed analysis.asset_requirements, not a product_data key.
        elicited = await _emit_asset_upload_prompt(
            stream, analysis.asset_requirements, session_memory, url)

        result_data: dict = {"product": product}
        if elicited:
            # deferred elicit → break loop, yield turn. multi = uploads span msgs.
            # requirements ride elicit_payload → _pending_elicitation.payload;
            # _asset_store decrements.
            result_data["elicited"] = True
            result_data["elicit_expects"] = "multi"
            if analysis.asset_requirements:
                result_data["elicit_payload"] = analysis.asset_requirements.to_dict()

        return ToolResult(
            success=True,
            data=result_data,
            summary=_build_llm_summary(product),
        )

    except Exception as e:
        logger.warning("analyze_product failed: %s: %s",
                       type(e).__name__, str(e)[:200])
        await _safe_emit_finished(stream, status="error", summary=str(e)[:100])
        return ToolResult(
            success=False,
            error=f"Product analysis failed: {type(e).__name__}: {e}",
        )

# ── Tool definition ───────────────────────────────────────────────────────────
analyze_business = ToolDefinition(
    name="analyze_product",
    description=(
        "Scrape a business website and generate a product profile: what they sell "
        "(all variants), location, pricing, USPs, target customer. This is the FIRST "
        "step when the user gives a URL. Does NOT do competitor research - that "
        "happens separately later. Returns a structured product profile."
    ),
    display_name="Analyze Product",
    parameters=[
        ToolParameter(name="url", type="string", description="The business website URL to analyze.", required=True),
    ],
    execute=_analyze_product,
)

BUSINESS_TOOLS = [analyze_business]

async def _run_product_agent(
    url: str,
    stream,
    tool_use_id: str,
    auth,
    session_ctx: dict,
):
    """Spawn the ProductAnalyst sub-agent and return its output.
    Peripheral I/O: agent startup, streaming events, and the user message."""
    from app.agents.adzump.agents.product.agent import get_product_agent
    from app.core.streaming import pre_emit_agent_started

    await emit_progress(session_ctx, "Starting product analysis…")
    # Launcher owns both AgentCard ends: agent_started here, agent_finished after.
    await pre_emit_agent_started(
        stream, agent_id="product_analyst", label="Product Analyst",
        parent_tool_use_id=tool_use_id, context=session_ctx,
    )
    return await get_product_agent().analyze(
        url=url,
        parent_event_stream=stream,
        parent_tool_use_id=tool_use_id,
        auth=auth,
        parent_session_context=session_ctx,
        user_message=f"Analyze this business website: {url}\n\n"
            f"SCOPE: Scrape the homepage ONCE and generate a product profile. "
            f"Do NOT scrape sub-pages - one scrape_url call only. "
            f"Do NOT call web_search or web_fetch. "
            f"After scraping, write the final JSON with the 'business' section filled "
            f"and an empty 'competitive' section.",
    )

# ── Context merge
def _merge_product_into_context(
    session_memory: dict,
    analysis,
    url: str,
) -> None:
    """Write analysis into session_memory:
    - product_data: merge not replace (keep runtime artifacts; JSON wins).
    - product_profile: url+title only (sub-agent owns .summary).
    - competitor_analysis: set if any."""
    product = dict(analysis.product or {})
    # The LLM emits `location` as a string (or {"location": str}); normalize it
    # into the nested `place` object here so session state has ONE shape.
    raw_loc = product.pop("location", "") or ""
    if isinstance(raw_loc, dict):
        raw_loc = raw_loc.get("location") or ""
    merged = {**(session_memory.get("product_data") or {}), **product}
    place = dict(merged.get("place") or {})
    if raw_loc.strip():
        place["address"] = raw_loc.strip()
    merged["place"] = place
    # The LLM→session boundary: warn on schema drift as it arrives, not at save.
    check_product(merged, where="merge_product")
    session_memory["product_data"] = merged
    profile = session_memory.setdefault("product_profile", {})
    profile["url"] = url
    profile["title"] = product.get("product_name", "") or profile.get("title", "")
    if not profile.get("summary"):   # seed only if sub-agent didn't run (legacy bypass)
        profile["summary"] = product.get("summary", "")
    if analysis.competitive and analysis.competitive.get("competitors"):
        session_memory["competitor_analysis"] = analysis.competitive


# ── Cross-session cache ──────────────────────────────────────────────────────

# TODO: no force-refresh path - both caches short-circuit unconditionally + the
# tool has no `force` param, so "re-scrape this site" can't bypass the cache.
# Add `force` → skip both caches + upsert (don't dup) the stored record.
async def _serve_from_storage(url: str, stream, context: dict, session_memory: dict) -> ToolResult | None:
    """Cross-session cache: if this URL was analyzed before, hydrate session_memory +
    re-render the craft panel (same UI as a fresh scrape) and return the reuse
    ToolResult. Returns None on miss/error - caller falls through to a fresh scrape."""
    try:
        from app.agents.adzump.services.business_storage import hydrate_from_storage
        from app.agents.adzump.tools.craft import emit_craft_panel as _emit_final_craft
        if not await hydrate_from_storage(url, session_memory, context):
            return None
        product = session_memory.get("product_data") or {}
        name = product.get("product_name", "product")
        await emit_progress(context, f"Reused stored analysis for {name}")

        craft_id = session_memory.get("craft_id", "")
        if stream and craft_id:
            competitive = session_memory.get("competitor_analysis") or {"competitors": []}
            try:
                await _emit_final_craft(
                    stream, craft_id, url, product, competitive,
                    screenshot_url=primary_screenshot_url(product),
                    baked_summary=(
                        (session_memory.get("product_profile") or {}).get("summary")
                        or product.get("summary", "")
                    ),
                )
            except Exception as e:
                logger.warning("storage_hydrate_craft_failed: %s: %s",
                               type(e).__name__, str(e)[:200])
        return ToolResult(
            success=True,
            data={"product": product, "from_storage": True},
            summary=(
                f"Reused prior analysis for {name} from storage. "
                f"Type: {product.get('business_type', '')}. "
                f"Location: {product.get('location', '')}. "
                "Tell the user we're picking up where things left off."
            ),
        )
    except Exception as e:
        logger.warning("storage_hydrate_skipped: %s: %s", type(e).__name__, str(e)[:200])
        return None


async def _emit_asset_upload_prompt(stream, requirements, session_memory: dict, url: str) -> bool:
    """Emit the asset-upload prompt if the picker left requirements open;
    no-op otherwise. `requirements` is the typed AnalysisOutput.asset_requirements.
    Returns True iff a prompt was emitted - caller flags the result as a
    (deferred, multi) elicitation so the run loop yields the turn."""
    if stream is None:
        return False
    if not (requirements and requirements.any_open()):   # nothing missing → nothing to ask
        return False

    missing_logo = requirements.logo_missing
    missing_creatives = list(requirements.missing_categories)

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
    # On session_memory so get_pending_suggestions returns it this turn - popped
    # before the elicitation guard, so the open upload elicitation can't suppress it.
    session_memory["_pending_suggestions"] = {
        "options": [{"label": "Continue without uploading",
                     "value": "Continue without uploading"}],
        "mode": "single",
    }

    logger.info(
        "asset_request_prompt_shown: url=%s stage=1 missing_logo=%s missing_creatives=%s",
        url, missing_logo, missing_creatives,
    )
    return True

def _build_llm_summary(product: dict) -> str:
    """The tool-result summary the orchestrator LLM sees. Deliberately OMITs location
    - echoing it made the LLM ask "confirm location?" as free text before
    confirm_location's widget fired (dup question); it stays in product_data."""
    lines: list[str] = []
    if product.get("product_name"):
        lines.append(f"Product: {product['product_name']}")
    if product.get("business_type"):
        lines.append(f"Type: {product['business_type']}")
    if product.get("summary"):
        lines.append(f"\n{product['summary']}")
    return "\n".join(lines) or "Product analysis complete."

async def _safe_emit_finished(stream, **kwargs) -> None:
    """emit_agent_finished for product_analyst, swallowing stream errors."""
    if stream is None:
        return
    try:
        await stream.emit_agent_finished(agent_id="product_analyst", **kwargs)
    except Exception:
        logger.debug("safe_emit_finished skipped: stream=%s kwargs=%s", stream, kwargs)

def _normalize_url(url: str) -> str:
    url = url.strip()
    if url.startswith("http://"):
        return "https://" + url[7:]
    if not url.startswith("https://"):
        return f"https://{url}"
    return url