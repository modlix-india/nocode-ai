"""Product analysis tool — runs the ProductAnalyst sub-agent to scrape + profile
a business website. The sub-agent owns the live scrape (Playwright + gpt-4o
profile generation streamed to the craft panel)."""

from __future__ import annotations

import logging

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.agents.adzump._shared import emit_progress

logger = logging.getLogger(__name__)


async def _analyze_product(params: dict, context: dict) -> ToolResult:
    """Spawn the Product Analyst agent to scrape + generate product profile.

    Thin bridge: cache check → spawn agent → persist → return.
    """
    import time as _time
    _run_start = _time.monotonic()

    url = (params.get("url") or "").strip()
    if not url:
        return ToolResult(success=False, error="url is required.")
    # Force https:// at the input boundary so storage records, complete-event
    # payloads, and downstream API calls are all consistent. The previous
    # `not url.startswith("http")` check accepted `http://` as-is, leaving
    # records keyed under both schemes for the same business.
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    elif not url.startswith("https://"):
        url = f"https://{url}"

    stream = context.get("event_stream")
    tool_use_id = context.get("tool_use_id", "")
    auth = context.get("auth")
    session_ctx = context.get("session_context", {}) or {}

    # Return cached results if already analyzed.
    parent_session = context.get("_session")
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

    # Cross-session storage cache: same URL was analyzed in a previous session
    # (by adzump or ds/chatv2). Hydrate session.context and skip the scrape.
    try:
        from app.agents.adzump.services.business_storage import hydrate_from_storage
        from app.agents.adzump.tools.competitor import _emit_final_craft
        target_ctx = parent_session.context if parent_session else session_ctx
        hit = await hydrate_from_storage(url, target_ctx, context)
        if hit:
            business = target_ctx.get("product_data") or {}
            name = business.get("product_name", "product")
            await emit_progress(context, f"Reused stored analysis for {name}")

            # Re-render the craft panel with the hydrated data so the user
            # sees the same screenshot + summary + competitors UI they'd
            # get on a fresh scrape.
            craft_id = target_ctx.get("craft_id", "")
            if stream and craft_id:
                competitive = target_ctx.get("competitor_analysis") or {"competitors": []}
                try:
                    await _emit_final_craft(
                        stream, craft_id, url, business, competitive,
                        screenshot_url=(
                            business.get("primary_screenshot_url")
                            or business.get("screenshot_url")
                        ),
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

    if auth is None:
        return ToolResult(
            success=False,
            error="No auth context — the ProductAnalyst sub-agent needs auth to run.",
        )

    try:
        from app.agents.adzump.agents.product.agent import get_product_agent

        await emit_progress(context, "Starting product analysis…")
        output = await get_product_agent().analyze(
            url=url,
            parent_event_stream=stream,
            parent_tool_use_id=tool_use_id,
            auth=auth,
            parent_session_context=session_ctx,
            user_message=(
                f"Analyze this business website: {url}\n\n"
                "SCOPE: Scrape the homepage ONCE and generate a product profile. "
                "Do NOT scrape sub-pages — one scrape_url call only. "
                "Do NOT call web_search or web_fetch. "
                "After scraping, write the final JSON with the 'business' section filled "
                "and an empty 'competitive' section."
            ),
        )

        if not output.business:
            raise RuntimeError("Agent produced no usable result")

        business = output.business
        if output.screenshot_url:
            business["screenshot_url"] = output.screenshot_url

        # Persist to parent session.
        #
        # Ownership:
        #   product_data       — shared between this tool and the scrape sub-agent.
        #                        The sub-agent populates runtime artifacts during
        #                        scraping (site_links, primary_screenshot_url,
        #                        scrape_count, scraped_urls); this tool merges the
        #                        analyst's structured JSON on top. Keys in the JSON
        #                        (product_name, summary, business_type, etc.) win
        #                        on conflict; runtime-only keys are preserved. Do
        #                        NOT replace product_data wholesale — that wipes
        #                        the sub-agent's artifacts (site_links was empty
        #                        in storage records because of this).
        #   product_profile    — owned by the scrape sub-agent. Wholesale-set in
        #                        agents/product/tools/scrape.py:_generate_business_profile
        #                        with a rich, streamed GPT-4o marketing summary.
        #                        We only refresh url + title from the analysis
        #                        output; summary stays under the sub-agent's
        #                        ownership and must not be overwritten here.
        #   competitor_analysis — owned by this tool when competitive output exists.
        parent_session = context.get("_session")
        target_ctx = parent_session.context if parent_session else session_ctx
        target_ctx["product_data"] = {**(target_ctx.get("product_data") or {}), **business}
        profile = target_ctx.setdefault("product_profile", {})
        profile["url"] = url
        profile["title"] = business.get("product_name", "") or profile.get("title", "")
        # Seed summary only if the sub-agent didn't run (defensive fallback for
        # legacy callers that bypass the sub-agent). The storage save's own
        # fallback chain `profile.summary or product.summary` covers the same
        # case at a different layer; keeping this here too means downstream
        # readers of product_profile see something useful regardless of layer.
        if not profile.get("summary"):
            profile["summary"] = business.get("summary", "")
        if output.competitive and output.competitive.get("competitors"):
            target_ctx["competitor_analysis"] = output.competitive

        # Build summary for the LLM.
        summary_lines: list[str] = []
        if business.get("product_name"):
            summary_lines.append(f"Product: {business['product_name']}")
        if business.get("business_type"):
            summary_lines.append(f"Type: {business['business_type']}")
        # v8 Plan B WS3 (Bug B) · deliberately OMIT the detected location from
        # this tool-result summary. Including it primed the LLM to echo
        # "please confirm the location for X" as free text before
        # confirm_location's own widget fired (the hiranandani duplicate). The
        # location stays in product_data; confirm_location reads it from there,
        # and the dynamic-context _missing_section still drives the location step.
        if business.get("summary"):
            summary_lines.append(f"\n{business['summary']}")

        duration_ms = int((_time.monotonic() - _run_start) * 1000)
        if stream:
            try:
                await stream.emit_agent_finished(
                    agent_id="product_analyst",
                    status="success",
                    duration_ms=duration_ms,
                    summary=f"Analyzed {business.get('product_name', 'product')}",
                )
            except Exception:
                pass

        # Shift 3 Stage 1 chat prompt (v9 live-test fix 2 · 2026-05-22).
        # Reads the decline signal that _persist_product_assets stashed on
        # product_data and emits the chat-text + structured data event on the
        # AdPilot PARENT stream — bypassing the sub-agent's
        # _PassthroughEventStream which drops emit_text.
        elicited = await _emit_asset_upload_prompt(stream, target_ctx, url)

        result_data: dict = {"business": business}
        if elicited:
            # v8 Plan B WS3 · conditional deferred elicitation. We asked the
            # user to upload missing assets — signal the run loop to break and
            # yield the turn. expects="multi": uploads may span several messages.
            result_data["elicited"] = True
            result_data["elicit_expects"] = "multi"

        return ToolResult(
            success=True,
            data=result_data,
            summary="\n".join(summary_lines) or "Product analysis complete.",
        )

    except Exception as e:
        logger.warning("analyze_product failed: %s: %s",
                       type(e).__name__, str(e)[:200])
        if stream:
            try:
                await stream.emit_agent_finished(
                    agent_id="product_analyst",
                    status="error",
                    summary=str(e)[:100],
                )
            except Exception:
                pass
        return ToolResult(
            success=False,
            error=f"Product analysis failed: {type(e).__name__}: {e}",
        )


# ── Shift 3 Stage 1 · upload-request chat prompt (orchestrator layer) ─────
#
# v9 live-test surfaced that the picker's inner _emit_asset_request_prompt
# was being dropped by ProductAgent._PassthroughEventStream.emit_text. The
# prompt is now emitted here, at the AdPilot tool wrapper layer, where
# `stream` IS the user-visible chat stream. _persist_product_assets stashes
# the signal on product_data["_shift3_signal"]; we read it and emit.

async def _emit_asset_upload_prompt(stream, target_ctx: dict, url: str) -> bool:
    """Emit Shift 3 Stage 1 chat prompt if picker left gaps. No-op when
    nothing missing or no stream. Telemetry event #1 of 4 (Lance's panel
    ask) — events 2-4 are nocode-ui responsibility.

    Returns True iff a user-facing upload prompt was actually emitted — the
    caller uses this to flag the tool result as a (deferred, multi) elicitation
    so the run loop yields the turn (v8 Plan B WS3)."""
    if stream is None:
        return False
    signal = (target_ctx.get("product_data") or {}).get("_shift3_signal") or {}
    missing_logo = bool(signal.get("logo_missing"))
    missing_creatives = list(signal.get("creative_missing_categories") or [])
    if not missing_logo and not missing_creatives:
        return False  # nothing to ask · picker output is complete

    # Reuse the composer from the picker layer · pure function, unit-tested.
    from app.agents.adzump.agents.product.tools.scrape.assets import (
        _compose_asset_request_text,
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

    # v3 · F7 — give the asset gate a quick-action chip so the user isn't forced
    # to type to continue (uploading is optional). Untagged, single-select;
    # clicking it sends the text and the LLM proceeds. Set on target_ctx (the
    # session context) so get_pending_suggestions returns it this turn — it's
    # popped before the _pending_elicitation guard, so the open multi-upload
    # elicitation doesn't suppress it.
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
